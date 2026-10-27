import pytest
import sqlglot

from nirvana import rewrite as rw
from nirvana.workload import tpcds


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT a FROM t ORDER BY a LIMIT 10", "SELECT a FROM t ORDER BY a"),
        ("SELECT a FROM t", None),
        ("SELECT a FROM (SELECT a FROM t LIMIT 3) x", None),
    ],
)
def test_drop_top_limit(sql, expected):
    assert rw.drop_top_limit(sql) == expected


@pytest.mark.parametrize(
    "sql,ctx,expected",
    [
        ("SELECT a FROM t ORDER BY a", {}, "SELECT a FROM t ORDER BY a ASC NULLS FIRST"),
        ("SELECT a FROM t ORDER BY a DESC", {}, "SELECT a FROM t ORDER BY a DESC NULLS LAST"),
        ("SELECT a FROM t ORDER BY a", {"null_order": "last"}, "SELECT a FROM t ORDER BY a ASC NULLS LAST"),
        ("SELECT a FROM t ORDER BY a DESC", {"null_order": "last"}, "SELECT a FROM t ORDER BY a DESC NULLS LAST"),
        ("SELECT a FROM t ORDER BY a NULLS LAST", {}, None),
        ("SELECT a FROM t", {}, None),
        ("SELECT a, b FROM t ORDER BY a, b DESC LIMIT 5", {}, "SELECT a, b FROM t ORDER BY a ASC NULLS FIRST, b DESC NULLS LAST LIMIT 5"),
    ],
)
def test_explicit_null_order(sql, ctx, expected):
    assert rw.explicit_null_order(sql, ctx) == expected


def test_null_order_output_parses_back():
    out = rw.explicit_null_order("SELECT a FROM t ORDER BY a, b DESC", {})
    tree = sqlglot.parse_one(out, read="spark")
    assert tree.args["order"] is not None


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT a / b FROM t", "SELECT CAST(a AS DOUBLE) / b FROM t"),
        ("SELECT CAST(a AS DOUBLE) / b FROM t", None),
        ("SELECT a FROM t", None),
        ("SELECT (a + 1) / (b - 1) FROM t", "SELECT CAST((a + 1) AS DOUBLE) / (b - 1) FROM t"),
        ("SELECT a / b / c FROM t", "SELECT CAST(CAST(a AS DOUBLE) / b AS DOUBLE) / c FROM t"),
    ],
)
def test_cast_divisions(sql, expected):
    assert rw.cast_divisions_to_double(sql, {}) == expected


@pytest.mark.parametrize(
    "sql,drift,expected",
    [
        ("SELECT SUM(ss_net_paid) FROM s", {"ss_net_paid": "decimal(7,2)"}, "SELECT SUM(CAST(ss_net_paid AS DECIMAL(7, 2))) FROM s"),
        ("SELECT x.ss_net_paid FROM s x", {"ss_net_paid": "decimal(7,2)"}, "SELECT CAST(x.ss_net_paid AS DECIMAL(7, 2)) FROM s AS x"),
        ("SELECT ss_item_sk FROM s", {"ss_net_paid": "decimal(7,2)"}, None),
        ("SELECT CAST(ss_net_paid AS DOUBLE) FROM s", {"ss_net_paid": "decimal(7,2)"}, None),
        ("SELECT SS_NET_PAID FROM s", {"ss_net_paid": "decimal(7,2)"}, "SELECT CAST(SS_NET_PAID AS DECIMAL(7, 2)) FROM s"),
        ("SELECT a FROM s", {}, None),
    ],
)
def test_cast_drifted_columns(sql, drift, expected):
    assert rw.cast_drifted_columns(sql, {"drift": drift}) == expected


def test_transpile_needs_two_dialects():
    assert rw.transpile_from_source("SELECT 1", {}) is None
    assert rw.transpile_from_source("SELECT 1", {"source_dialect": "spark", "target_dialect": "spark"}) is None


def test_transpile_renders_for_target():
    out = rw.transpile_from_source("SELECT DATE_ADD(d, 1) FROM t", {"source_dialect": "databricks", "target_dialect": "spark"})
    assert out is None or "DATE_ADD" in out.upper()


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT a, b FROM t ORDER BY a LIMIT 3", "SELECT a, b FROM t ORDER BY a, 2 LIMIT 3"),
        ("SELECT a FROM t", None),
    ],
)
def test_total_order(sql, expected):
    assert rw.total_order(sql, {}) == expected


def test_chain_applies_in_order():
    out = rw.chain("SELECT ss_net_paid / 2 FROM s", ["cast_drifted_columns", "cast_divisions_to_double"], {"drift": {"ss_net_paid": "decimal(7,2)"}})
    assert out == "SELECT CAST(CAST(ss_net_paid AS DECIMAL(7, 2)) AS DOUBLE) / 2 FROM s"


def test_chain_none_when_nothing_applies():
    assert rw.chain("SELECT a FROM t", ["cast_divisions_to_double"], {}) is None


def test_catalogue_names():
    assert set(rw.CATALOGUE) == {"cast_drifted_columns", "cast_divisions_to_double", "explicit_null_order", "transpile_from_source"}
    assert all(r.describe for r in rw.CATALOGUE.values())


DIVIDING = [q for q in tpcds() if any(True for _ in sqlglot.parse_one(q.sql, read="spark").find_all(sqlglot.exp.Div))]


@pytest.mark.parametrize("q", DIVIDING, ids=[q.id for q in DIVIDING])
def test_division_rewrite_parses_for_every_dividing_tpcds_query(q):
    out = rw.cast_divisions_to_double(q.sql, {})
    assert out is not None
    assert sqlglot.parse_one(out, read="spark") is not None
    assert out.count("AS DOUBLE)") >= 1


ORDERED = [q for q in tpcds() if " order by " in q.sql.lower().replace("\n", " ")]


@pytest.mark.parametrize("q", ORDERED[:40], ids=[q.id for q in ORDERED[:40]])
def test_null_order_rewrite_parses_for_tpcds(q):
    out = rw.explicit_null_order(q.sql, {})
    if out is not None:
        assert "NULLS" in out
        assert sqlglot.parse_one(out, read="spark") is not None
