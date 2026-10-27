"""Engine clients against a local HTTP double that speaks each vendor's documented shapes,
plus DuckDB as a real engine."""

import json
from decimal import Decimal

import httpx
import pytest

from nirvana.engines import DatabricksEngine, DuckDBEngine, ZetarisEngine, rehearsal_pair
from nirvana.engines.base import columns_from_names, infer_type
from nirvana.engines.zetaris import parse_payload
from nirvana.model import EngineError


# ---------------------------------------------------------------- Databricks


def dbx_manifest(cols):
    return {"schema": {"columns": [{"name": n, "type_text": t, "position": i} for i, (n, t) in enumerate(cols)]}}


def dbx(handler, **kw):
    return DatabricksEngine("example.cloud.databricks.com", "tok", "wh1", transport=httpx.MockTransport(handler), poll_seconds=0, **kw)


def test_databricks_inline_success():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={
            "statement_id": "s1",
            "status": {"state": "SUCCEEDED"},
            "manifest": dbx_manifest([("a", "INT"), ("b", "DECIMAL(7,2)")]),
            "result": {"data_array": [["1", "2.50"], ["2", None]]},
        })

    rs = dbx(handler).run("SELECT 1", max_rows=10)
    assert rs.column_names == ["a", "b"]
    assert [c.type for c in rs.columns] == ["int", "decimal(7,2)"]
    assert rs.rows == (("1", "2.50"), ("2", None))
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["warehouse_id"] == "wh1"
    assert seen["body"]["format"] == "JSON_ARRAY"
    assert seen["body"]["disposition"] == "INLINE"
    assert seen["body"]["row_limit"] == 11
    assert rs.meta["statement_id"] == "s1"


def test_databricks_host_gets_scheme():
    e = dbx(lambda r: httpx.Response(200, json={}))
    assert e.host == "https://example.cloud.databricks.com"


def test_databricks_polls_until_done():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(200, json={"statement_id": "s9", "status": {"state": "PENDING"}})
        if len(calls) < 3:
            return httpx.Response(200, json={"statement_id": "s9", "status": {"state": "RUNNING"}})
        return httpx.Response(200, json={"statement_id": "s9", "status": {"state": "SUCCEEDED"}, "manifest": dbx_manifest([("x", "BIGINT")]), "result": {"data_array": [["7"]]}})

    rs = dbx(handler).run("SELECT 7")
    assert rs.rows == (("7",),)
    assert calls[0] == ("POST", "/api/2.0/sql/statements")
    assert calls[-1] == ("GET", "/api/2.0/sql/statements/s9")


def test_databricks_follows_chunks():
    def handler(req):
        if req.url.path.endswith("/result/chunks/1"):
            return httpx.Response(200, json={"data_array": [["3"]], "next_chunk_internal_link": None})
        return httpx.Response(200, json={
            "statement_id": "s2", "status": {"state": "SUCCEEDED"}, "manifest": dbx_manifest([("x", "INT")]),
            "result": {"data_array": [["1"], ["2"]], "next_chunk_internal_link": "/api/2.0/sql/statements/s2/result/chunks/1"},
        })

    assert dbx(handler).run("q").rows == (("1",), ("2",), ("3",))


def test_databricks_truncation_flag():
    def handler(req):
        return httpx.Response(200, json={"statement_id": "s", "status": {"state": "SUCCEEDED"}, "manifest": dbx_manifest([("x", "INT")]), "result": {"data_array": [["1"], ["2"], ["3"]]}})

    rs = dbx(handler).run("q", max_rows=2)
    assert rs.truncated and rs.row_count == 2


def test_databricks_failure_message_is_kept():
    def handler(req):
        return httpx.Response(200, json={"statement_id": "s", "status": {"state": "FAILED", "error": {"message": "[UNRESOLVED_COLUMN] x"}}})

    with pytest.raises(EngineError, match="UNRESOLVED_COLUMN"):
        dbx(handler).run("q")


@pytest.mark.parametrize("code", [401, 403])
def test_databricks_auth_refused(code):
    with pytest.raises(EngineError, match="token was refused"):
        dbx(lambda r: httpx.Response(code)).run("q")


def test_databricks_http_error():
    with pytest.raises(EngineError, match="HTTP 500"):
        dbx(lambda r: httpx.Response(500, text="boom")).run("q")


def test_databricks_network_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(EngineError, match="cannot reach"):
        dbx(handler).run("q")


def test_databricks_timeout_cancels():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"statement_id": "s", "status": {"state": "RUNNING"}})

    with pytest.raises(EngineError, match="did not finish"):
        dbx(handler, timeout_seconds=0).run("q")
    assert calls[-1].endswith("/cancel")


def test_databricks_missing_statement_id():
    with pytest.raises(EngineError, match="no statement id"):
        dbx(lambda r: httpx.Response(200, json={"status": {"state": "PENDING"}})).run("q")


@pytest.mark.parametrize("args", [("", "t", "w"), ("h", "", "w"), ("h", "t", "")])
def test_databricks_requires_settings(args):
    with pytest.raises(ValueError):
        DatabricksEngine(*args)


def test_databricks_ping_ok():
    def handler(req):
        return httpx.Response(200, json={"statement_id": "s", "status": {"state": "SUCCEEDED"}, "manifest": dbx_manifest([("one", "INT")]), "result": {"data_array": [["1"]]}})

    st = dbx(handler).ping()
    assert st.ok and st.engine == "databricks"


def test_databricks_ping_reports_error():
    st = dbx(lambda r: httpx.Response(401)).ping()
    assert not st.ok and "refused" in st.detail


def test_databricks_describe_has_no_token():
    d = dbx(lambda r: httpx.Response(200, json={})).describe()
    assert "tok" not in json.dumps(d)
    assert d["warehouse_id"] == "wh1" and d["location"] == "samples.tpcds_sf1"


# ---------------------------------------------------------------- Zetaris


def zet(handler, **kw):
    return ZetarisEngine("key", "7", "http://zetaris.local:8888/api/v1.0/", transport=httpx.MockTransport(handler), **kw)


def test_zetaris_request_shape():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": [[1]], "columns": [{"name": "one", "type": "int"}]})

    rs = zet(handler).run("SELECT 1", max_rows=5)
    assert seen["url"] == "http://zetaris.local:8888/api/v1.0/sql-editor/sqls/run"
    assert seen["headers"]["authorization"] == "Bearer key"
    assert seen["headers"]["x-org-id"] == "7"
    assert len(seen["headers"]["x-request-id"]) == 36
    assert seen["body"]["sql"] == "SELECT 1" and seen["body"]["source"] == "SqlEditor" and seen["body"]["limit"] == 6
    assert rs.rows == ((1,),) and rs.columns[0].type == "int"
    assert rs.meta["types_inferred"] is False


@pytest.mark.parametrize(
    "payload,names,types,rows",
    [
        ({"data": [[1, "a"]], "columns": ["x", "y"]}, ["x", "y"], None, [[1, "a"]]),
        ({"data": [{"x": 1, "y": "a"}]}, ["x", "y"], None, [[1, "a"]]),
        ({"data": [[1]], "schema": [{"name": "x", "dataType": "bigint"}]}, ["x"], ["bigint"], [[1]]),
        ({"data": {"rows": [[2]], "columns": [{"name": "x", "type": "int"}]}}, ["x"], ["int"], [[2]]),
        ({"data": [[1, 2]]}, ["col0", "col1"], None, [[1, 2]]),
        ({"data": []}, [], None, []),
        ({"data": [[1]], "metadata": {"fields": [{"name": "m", "typeName": "double"}]}}, ["m"], ["double"], [[1]]),
        ({"data": [[1]], "header": [{"label": "h"}]}, ["h"], None, [[1]]),
    ],
)
def test_zetaris_payload_shapes(payload, names, types, rows):
    assert parse_payload(payload) == (names, types, rows)


@pytest.mark.parametrize("payload", [[1, 2], "x", {"error": "Table not found"}, {"errorMessage": "bad"}])
def test_zetaris_payload_errors(payload):
    with pytest.raises(ValueError):
        parse_payload(payload)


def test_zetaris_inferred_types_marked():
    rs = zet(lambda r: httpx.Response(200, json={"data": [[1, 2.5, "x", None]]})).run("q")
    assert [c.type for c in rs.columns] == ["bigint", "double", "string", "string"]
    assert rs.meta["types_inferred"] is True


def test_zetaris_engine_error_text_kept():
    with pytest.raises(EngineError, match="Table not found"):
        zet(lambda r: httpx.Response(200, json={"error": "Table not found"})).run("q")


@pytest.mark.parametrize("code,match", [(401, "API key was refused"), (400, "ZETARIS_ORG_ID"), (500, "HTTP 500")])
def test_zetaris_http_errors(code, match):
    text = "invalid org" if code == 400 else "x"
    with pytest.raises(EngineError, match=match):
        zet(lambda r: httpx.Response(code, text=text)).run("q")


def test_zetaris_unreachable():
    def handler(req):
        raise httpx.ConnectError("no")

    with pytest.raises(EngineError, match="cannot reach"):
        zet(handler).run("q")


def test_zetaris_truncation():
    rs = zet(lambda r: httpx.Response(200, json={"data": [[1], [2], [3]], "columns": ["x"]})).run("q", max_rows=2)
    assert rs.truncated and rs.row_count == 2


@pytest.mark.parametrize("kw", [{"api_key": ""}, {"org_id": "abc"}])
def test_zetaris_requires_settings(kw):
    args = {"api_key": "k", "org_id": "1"}
    args.update(kw)
    with pytest.raises(ValueError):
        ZetarisEngine(**args)


def test_zetaris_describe_has_no_key():
    d = zet(lambda r: httpx.Response(200)).describe()
    assert "key" not in json.dumps({k: v for k, v in d.items() if k != "rest_url"}).replace("location", "")
    assert d["rest_url"] == "http://zetaris.local:8888/api/v1.0"


# ---------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "values,expected",
    [([1, 2], "bigint"), ([1, 2.5], "double"), ([1.5], "double"), (["a"], "string"), ([None], "string"), ([], "string"), ([True, False], "boolean"), ([1, "a"], "string")],
)
def test_infer_type(values, expected):
    assert infer_type(values) == expected


def test_columns_from_names_checks_lengths():
    with pytest.raises(ValueError):
        columns_from_names(["a"], [], ["int", "int"])


# ---------------------------------------------------------------- DuckDB (real engine)


def test_duckdb_runs_spark_sql():
    e = DuckDBEngine()
    rs = e.run("SELECT CAST(7 AS INT) / CAST(2 AS INT) AS r")
    assert rs.rows == ((3.5,),)
    assert rs.columns[0].type == "double"


def test_duckdb_error_is_engine_error():
    with pytest.raises(EngineError):
        DuckDBEngine().run("SELECT * FROM no_such_table")


def test_duckdb_truncation():
    rs = DuckDBEngine().run("SELECT * FROM range(10)", max_rows=3)
    assert rs.truncated and rs.row_count == 3


def test_rehearsal_pair_types_differ_but_rows_match():
    src, tgt = rehearsal_pair(0.01)
    a = src.run("SELECT ss_net_paid FROM main.store_sales ORDER BY ss_item_sk, ss_ticket_number LIMIT 3")
    b = tgt.run("SELECT ss_net_paid FROM drift.store_sales ORDER BY ss_item_sk, ss_ticket_number LIMIT 3")
    assert a.columns[0].type.startswith("decimal")
    assert b.columns[0].type == "double"
    assert [Decimal(str(x[0])) for x in a.rows] == [Decimal(str(x[0])) for x in b.rows]
    assert src.run("SELECT count(*) FROM main.store_sales").rows == tgt.run("SELECT count(*) FROM drift.store_sales").rows
    assert tgt.location == "drift" and src.location == "main"


def test_duckdb_ping():
    assert DuckDBEngine().ping().ok
