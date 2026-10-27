"""Guard rules: derived from the source's profile, installed and run as Zetaris DQ rules."""

import duckdb
import httpx
import pytest

from nirvana.engines.duck import DuckDBEngine
from nirvana.engines.zetaris import ZetarisEngine
from nirvana.guard import (
    NAMESPACE, Guard, GuardStore, Rule, build_guard, compile_sql, derive_rules, ident, profile_sql, usl_name, usl_type,
)
from nirvana.model import Column


@pytest.mark.parametrize("t,want", [("integer", "int"), ('"long"', "bigint"), ("double", "double"), ("decimal(7,2)", "decimal(7,2)"),
                                    ("date", "date"), ("string", "varchar(1000)"), ("varchar(16)", "varchar(1000)"), ("smallint", "int")])
def test_usl_types(t, want):
    assert usl_type(t) == want


@pytest.mark.parametrize("bad", ["a b", "1x", "x;y", "", "a.b"])
def test_identifiers(bad):
    with pytest.raises(ValueError):
        ident(bad)


COLS = [Column("k", "integer"), Column("zip", "string"), Column("price", "decimal(7,2)"), Column("d", "date"), Column("note", "string")]


def test_profile_sql_shape():
    sql = profile_sql("db.t", COLS)
    assert sql.startswith("SELECT COUNT(*) AS n_rows, COUNT(`k`) AS c0_n")
    assert "MIN(LENGTH(`zip`)) AS c1_lo" in sql and "CAST(MAX(`price`) AS STRING) AS c2_hi" in sql
    assert sql.endswith("FROM db.t")


def test_rules_from_a_profile():
    vals = [10, 10, 1, 9, 10, 5, 5, 8, "0.50", "9.99", 10, "1998-01-01", "2003-12-31", 0, None, None]
    rules = {r.column: r for r in derive_rules(COLS, vals)}
    assert rules["k"].expression == "k IS NOT NULL AND k BETWEEN 1 AND 9"
    assert rules["zip"].expression == "zip IS NOT NULL AND length(cast(zip AS string)) = 5"
    assert rules["price"].expression == "price IS NULL OR (price BETWEEN 0.50 AND 9.99)"
    assert rules["d"].expression == "d IS NOT NULL AND d BETWEEN DATE '1998-01-01' AND DATE '2003-12-31'"
    assert "note" not in rules  # always NULL: nothing to say
    assert "5 characters" in rules["zip"].why and "never NULL" in rules["zip"].why


def test_odd_literals_are_not_written_into_rules():
    rules = derive_rules([Column("p", "double")], [3, 3, "1e-5", "NaN"])
    assert rules[0].expression == "p IS NOT NULL"


def test_names():
    assert usl_name("customer_address", "NIRVANA_CSV") == "guard_nirvana_csv_customer_address"
    sql = compile_sql("g", "t", [Column("a", "integer"), Column("b", "string")])
    assert sql == f"COMPILE USL IF NOT EXISTS g DEPLOY NAMESPACE {NAMESPACE} DDL\nCREATE TABLE t (a int, b varchar(1000))"


@pytest.fixture(scope="module")
def src():
    c = duckdb.connect()
    c.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1,'02134'),(2,'94110'),(3,'00501')) v(k, zip)")
    return DuckDBEngine("src", "main", connection=c.cursor())


class FakeZetaris:
    """Answers the USL/DQ statements the way the live 2.4.3 build does; evaluates rules on rows."""

    name, dialect, null_order = "zetaris", "spark", "spark"

    def __init__(self, rows, fail_register=False):
        self.rows, self.fail_register, self.sql, self.rules = rows, fail_register, [], {}

    def run(self, sql, *, max_rows=100_000):
        from nirvana.model import EngineError, ResultSet

        self.sql.append(sql)
        cols = [Column("k", "integer"), Column("zip", "integer")]
        if sql.endswith("LIMIT 0"):
            return ResultSet.build("zetaris", sql, cols, [])
        if sql.startswith("REGISTER DQ"):
            if self.fail_register:
                raise EngineError("zetaris", "rule already exists")
            name = sql.split()[2]
            self.rules[name] = sql.split(" AS ", 1)[1]
        if sql.startswith("UPDATE DQ"):
            self.rules[sql.split()[2]] = sql.split("EXPRESSION = ", 1)[1]
        if sql.startswith("RUN DQ") or sql.startswith("SHOW DQ INVALID"):
            name = sql.split()[2] if sql.startswith("RUN") else sql.split()[4]
            expr = self.rules[name]
            bad = [r for r in self.rows if "zip" in name and len(str(r[1])) != 5]
            if sql.startswith("RUN"):
                c = [Column(n, "string") for n in ("name", "table", "type", "total_record", "valid_record", "invalid_record")]
                return ResultSet.build("zetaris", sql, c, [(name, "t", "Custom Data Quality", str(len(self.rows)), str(len(self.rows) - len(bad)), str(len(bad)))])
            return ResultSet.build("zetaris", sql, cols, bad)
        return ResultSet.build("zetaris", sql, [], [])


def test_guard_catches_lost_leading_zeros(src):
    z = FakeZetaris([(1, 2134), (2, 94110), (3, 501)])
    g = build_guard(src, "main.t", z, "t", "CSV")
    assert g.error is None
    zip_rule = next(r for r in g.rules if r.column == "zip")
    assert zip_rule.expression == "zip IS NOT NULL AND length(cast(zip AS string)) = 5"
    assert (zip_rule.total, zip_rule.valid, zip_rule.invalid) == (3, 1, 2)
    assert zip_rule.sample == [[1, 2134], [3, 501]]
    assert any(s.startswith("COMPILE USL IF NOT EXISTS guard_csv_t") for s in g.sql)
    assert any(s.startswith(f"ACTIVATE USL TABLE {NAMESPACE}.guard_csv_t.t AS SELECT k, zip FROM CSV.t") for s in g.sql)
    assert g.as_dict()["broken"] == 1


def test_existing_rule_is_updated(src):
    z = FakeZetaris([(1, 2134)], fail_register=True)
    g = build_guard(src, "main.t", z, "t", "CSV")
    assert any(s.startswith("UPDATE DQ zip_shape") for s in g.sql)
    assert next(r for r in g.rules if r.column == "zip").invalid == 1


def test_column_filter(src):
    g = build_guard(src, "main.t", FakeZetaris([(1, 12345)]), "t", "CSV", columns=["zip"])
    assert [r.column for r in g.rules] == ["zip"]


def test_bad_candidate_name(src):
    g = build_guard(src, "main.t", FakeZetaris([]), "t", "C;SV")
    assert g.error.startswith("not a plain identifier")


def test_source_failure_is_reported(src):
    g = build_guard(src, "main.nope", FakeZetaris([]), "t", "CSV")
    assert g.error and not g.rules


def test_store(tmp_path):
    st = GuardStore(tmp_path / "g.jsonl")
    st.save(Guard("t", "s", "C", "u", [Rule("a", "a", "a IS NOT NULL", "x", 3, 2, 1)], at=1.0))
    st.save(Guard("t", "s", "C", "u", [], at=2.0))
    st.save(Guard("u", "s", "C", "u2", [], at=3.0))
    got = st.latest()
    assert [g["table"] for g in got] == ["u", "t"] and got[1]["rules"] == []


def test_zetaris_wire_format_for_run_dq():
    def handler(req):
        sql = req.read().decode()
        if "RUN DQ" in sql:
            return httpx.Response(200, json={"headers": ["name", "table", "type", "total_record", "valid_record", "invalid_record"],
                                             "data": [["ca_zip_shape", "customer_address", "Custom Data Quality", "50000", "48649", "1351"]]})
        return httpx.Response(200, json={"headers": ["x"], "data": []})

    z = ZetarisEngine("k", transport=httpx.MockTransport(handler))
    rs = z.run("RUN DQ ca_zip_shape TABLE lightning.metastore.nirvana.g.customer_address")
    assert rs.rows[0][3:] == ("50000", "48649", "1351") or list(rs.rows[0][3:]) == ["50000", "48649", "1351"]
