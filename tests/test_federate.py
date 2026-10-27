"""Row-level diffs computed by one federated query: SQL shape, counting, sampling, storage, API."""

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient

from nirvana.engines.duck import DuckDBEngine
from nirvana.engines.zetaris import ZetarisEngine
from nirvana.federate import DiffStore, RowDiff, diff_sql, except_all, row_diff


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute("CREATE SCHEMA ref; CREATE SCHEMA cand; CREATE SCHEMA same")
    c.execute("CREATE TABLE ref.t AS SELECT * FROM (VALUES (1,'a'),(2,'b'),(3,'c')) v(k,s)")
    c.execute("CREATE TABLE same.t AS SELECT * FROM ref.t")
    # candidate: row 2 missing, row 3 duplicated twice more, one new row
    c.execute("CREATE TABLE cand.t AS SELECT * FROM (VALUES (1,'a'),(3,'c'),(3,'c'),(3,'c'),(9,'z')) v(k,s)")
    return c


@pytest.fixture
def duck(con):
    return DuckDBEngine("duck", "main", connection=con.cursor())


def test_except_all_text():
    assert except_all("A.t", "B.t") == "SELECT * FROM A.t EXCEPT ALL SELECT * FROM B.t"


def test_diff_sql_runs_both_directions():
    q = diff_sql("store", "REF", "CAND", 3)
    assert "FROM REF.store EXCEPT ALL SELECT * FROM CAND.store" in q["count_reference"]
    assert "FROM CAND.store EXCEPT ALL SELECT * FROM REF.store" in q["count_candidate"]
    assert q["sample_candidate"].endswith("LIMIT 3")


@pytest.mark.parametrize("bad", ["x; DROP", "a.b", "t--", "a b", ""])
def test_identifiers_must_be_plain(bad):
    with pytest.raises(ValueError):
        diff_sql(bad, "R", "C", 1)
    with pytest.raises(ValueError):
        diff_sql("t", bad, "C", 1)


def test_counts_keep_duplicates(duck):
    d = row_diff(duck, "t", "ref", "cand")
    assert d.error is None
    assert d.only_in_reference == 1  # (2,'b')
    assert d.only_in_candidate == 3  # two extra (3,'c') and (9,'z')
    assert not d.identical


def test_samples_show_the_rows(duck):
    d = row_diff(duck, "t", "ref", "cand", sample=10)
    assert d.sample_reference == [[2, "b"]]
    assert sorted(map(tuple, d.sample_candidate)) == [(3, "c"), (3, "c"), (9, "z")]
    assert d.columns == ["k", "s"]


def test_identical_copies(duck):
    d = row_diff(duck, "t", "ref", "same")
    assert d.identical and d.only_in_reference == 0 and d.only_in_candidate == 0
    assert d.sample_reference == [] and d.sample_candidate == []


def test_four_statements_recorded(duck):
    assert len(row_diff(duck, "t", "ref", "cand").sql) == 4


def test_engine_error_is_reported_not_raised(duck):
    d = row_diff(duck, "nope", "ref", "cand")
    assert d.error and d.only_in_reference is None


def test_bad_identifier_is_an_error_without_running(duck):
    d = row_diff(duck, "t;", "ref", "cand")
    assert d.error.startswith("not a plain identifier") and d.sql == []


def test_against_the_zetaris_wire_format():
    seen = []

    def handler(req):
        sql = req.read().decode()
        seen.append(sql)
        if "COUNT(*)" in sql:
            n = "300190" if "FROM TPCDS_DB.catalog_page EXCEPT" in sql else "190"
            return httpx.Response(200, json={"headers": ["n"], "data": [[n]], "total": 1})
        if "INFER SQLTABLE" in sql:
            return httpx.Response(200, json={"headers": ["name", "dataType"], "data": [["cp_catalog_page_sk", '"long"']]})
        return httpx.Response(200, json={"headers": ["cp_catalog_page_sk"], "data": [["179"], ["179"]], "total": 2})

    z = ZetarisEngine("k", transport=httpx.MockTransport(handler))
    d = row_diff(z, "catalog_page", "TPCDS_DB", "NIRVANA_TPCDS", sample=2)
    assert d.only_in_reference == 300190 and d.only_in_candidate == 190
    assert d.sample_reference == [[179], [179]] or d.sample_reference == [["179"], ["179"]]


def test_store_keeps_the_latest_per_pair(tmp_path):
    st = DiffStore(tmp_path / "d.jsonl")
    assert st.latest() == []
    st.save(RowDiff("t", "A", "B", 1, 2, at=1.0))
    st.save(RowDiff("t", "A", "B", 5, 6, at=2.0))
    st.save(RowDiff("u", "A", "B", 0, 0, at=3.0))
    got = st.latest()
    assert [g["table"] for g in got] == ["u", "t"]
    assert got[1]["only_in_reference"] == 5 and got[0]["identical"] is True


def test_store_skips_broken_lines(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text("not json\n")
    DiffStore(p).save(RowDiff("t", "A", "B", 0, 0, at=1.0))
    assert len(DiffStore(p).latest()) == 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    from nirvana.api import create_app
    from nirvana.config import settings

    env = {"NIRVANA_HOME": str(tmp_path), "NIRVANA_RECORDINGS": str(tmp_path / "rec")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with TestClient(create_app(settings(env), mode="live")) as c:
        yield c


def test_api_lists_nothing_at_first(client):
    assert client.get("/api/diffs").json() == []


def test_api_rejects_unknown_table(client):
    assert client.post("/api/diffs", json={"table": "nope", "reference": "A", "candidate": "B"}).status_code == 400


def test_api_needs_zetaris_configured(client):
    r = client.post("/api/diffs", json={"table": "store", "reference": "A", "candidate": "B"})
    assert r.status_code == 400 and "Zetaris" in r.json()["detail"]


def test_api_recorded_mode_refuses_to_run(tmp_path, monkeypatch):
    from nirvana.api import create_app
    from nirvana.config import settings

    monkeypatch.setenv("NIRVANA_HOME", str(tmp_path))
    with TestClient(create_app(settings({"NIRVANA_HOME": str(tmp_path)}), mode="recorded")) as c:
        assert c.post("/api/diffs", json={"table": "store", "reference": "A", "candidate": "B"}).status_code == 409
