import duckdb
import pytest
import sqlglot

from nirvana import workload as w

QUERIES = w.tpcds()
IDS = [q.id for q in QUERIES]

# Two Spark renderings that DuckDB cannot run as written. Both are facts about the
# workload, kept here so a change in either is noticed:
#   q30 reads c_last_review_date, a column the TPC-DS generator names c_last_review_date_sk;
#   q90 uses `at` as a table alias, a reserved word in DuckDB.
DUCKDB_UNPORTABLE = {"q30", "q90"}


@pytest.fixture(scope="module")
def tpcds_duck():
    con = duckdb.connect()
    con.execute("INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf=0.01)")
    yield con
    con.close()


def test_workload_has_103_queries():
    assert len(QUERIES) == 103
    assert len(set(IDS)) == 103


def test_workload_order_is_numeric():
    assert IDS[:3] == ["q1", "q2", "q3"]
    assert IDS.index("q14a") + 1 == IDS.index("q14b")
    assert IDS[-1] == "q99"


def test_get_and_select():
    assert w.get("q42").id == "q42"
    assert [q.id for q in w.select(["q3", "q1"])] == ["q3", "q1"]
    assert len(w.select(None)) == 103
    with pytest.raises(KeyError):
        w.get("q100")


@pytest.mark.parametrize("q", QUERIES, ids=IDS)
def test_query_parses(q):
    assert w.parse(q.sql) is not None


@pytest.mark.parametrize("q", QUERIES, ids=IDS)
def test_query_reads_only_workload_tables(q):
    tables = w.tables_read(q.sql)
    assert tables, "every TPC-DS query reads at least one table"
    assert set(tables) <= w.TPCDS_TABLES


@pytest.mark.parametrize("q", QUERIES, ids=IDS)
def test_qualify_for_databricks_leaves_nothing_unqualified(q):
    out = w.qualify(q.sql, "samples.tpcds_sf1", "databricks")
    assert w.unqualified_tables(out, "databricks") == []
    for t in w.tables_read(q.sql):
        assert f"samples.tpcds_sf1.{t}" in out


@pytest.mark.parametrize("q", QUERIES, ids=IDS)
def test_qualify_for_zetaris_leaves_nothing_unqualified(q):
    out = w.qualify(q.sql, "TPCDS_DB", "spark")
    assert w.unqualified_tables(out) == []
    for t in w.tables_read(q.sql):
        assert f"TPCDS_DB.{t}" in out


@pytest.mark.parametrize("q", QUERIES, ids=IDS)
def test_qualify_is_idempotent(q):
    once = w.qualify(q.sql, "TPCDS_DB")
    assert w.qualify(once, "TPCDS_DB") == once


@pytest.mark.parametrize("q", [q for q in QUERIES if q.id not in DUCKDB_UNPORTABLE], ids=[i for i in IDS if i not in DUCKDB_UNPORTABLE])
def test_qualified_query_gives_same_rows_as_original_on_real_data(q, tpcds_duck):
    """Qualifying must not change the answer: run both texts on generated TPC-DS data."""
    original = sqlglot.transpile(q.sql, read="spark", write="duckdb")[0]
    qualified = sqlglot.transpile(w.qualify(q.sql, "main"), read="spark", write="duckdb")[0]
    a = tpcds_duck.execute(original).fetchall()
    b = tpcds_duck.execute(qualified).fetchall()
    assert sorted(map(repr, a)) == sorted(map(repr, b))


@pytest.mark.parametrize("qid", sorted(DUCKDB_UNPORTABLE))
def test_known_unportable_queries_still_fail_on_duckdb(qid, tpcds_duck):
    sql = sqlglot.transpile(w.get(qid).sql, read="spark", write="duckdb")[0]
    with pytest.raises(duckdb.Error):
        tpcds_duck.execute(sql).fetchall()


def test_cte_names_are_not_qualified():
    sql = "WITH store_sales AS (SELECT 1 AS x) SELECT x FROM store_sales"
    assert "TPCDS_DB" not in w.qualify(sql, "TPCDS_DB")


def test_already_qualified_tables_untouched():
    sql = "SELECT * FROM other.store_sales"
    assert w.qualify(sql, "TPCDS_DB") == "SELECT * FROM other.store_sales"


def test_non_workload_tables_untouched():
    assert w.qualify("SELECT * FROM my_table", "TPCDS_DB") == "SELECT * FROM my_table"


@pytest.mark.parametrize("loc,expected", [("TPCDS_DB", (None, "TPCDS_DB")), ("samples.tpcds_sf1", ("samples", "tpcds_sf1")), (" a.b ", ("a", "b"))])
def test_split_location(loc, expected):
    assert w.split_location(loc) == expected


@pytest.mark.parametrize("loc", ["", "a.b.c", "..."])
def test_split_location_rejects(loc):
    with pytest.raises(ValueError):
        w.split_location(loc)


@pytest.mark.parametrize(
    "sql,ordered,limit",
    [
        ("SELECT 1", False, None),
        ("SELECT a FROM t ORDER BY a", True, None),
        ("SELECT a FROM t ORDER BY a LIMIT 100", True, 100),
        ("SELECT a FROM (SELECT a FROM t ORDER BY a) x", False, None),
        ("SELECT a FROM t UNION ALL SELECT a FROM u ORDER BY a LIMIT 5", True, 5),
        ("WITH c AS (SELECT 1 a) SELECT a FROM c ORDER BY a", True, None),
    ],
)
def test_order_and_limit_detection(sql, ordered, limit):
    assert w.has_top_level_order(sql) is ordered
    assert w.top_level_limit(sql) == limit


def test_most_tpcds_queries_sort_their_output():
    assert sum(w.has_top_level_order(q.sql) for q in QUERIES) == 91


def test_query_hash_is_stable():
    q = w.get("q1")
    assert q.sha256 == w.Query("q1", q.sql).sha256
    assert len(q.sha256) == 64
