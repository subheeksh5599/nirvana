"""The judge on real engines: DuckDB connections over the same generated TPC-DS rows, each
target changed in one documented way (a session setting, a column type, the physical row
order). Nothing here imitates an engine; every result is executed."""

import pytest

from nirvana.align import align_table
from nirvana.engines import DuckDBEngine
from nirvana.judge import Judge, features
from nirvana.workload import Query, get

import duckdb


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("judge") / "tpcds.duckdb")
    con = duckdb.connect(path)
    con.execute("INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf=0.01)")
    yield con
    con.close()


@pytest.fixture(scope="module")
def source(db):
    return DuckDBEngine("source", "main", connection=db.cursor())


def target_with(db, name, *settings, read_dialect="spark", location="main"):
    cur = db.cursor()
    for s in settings:
        cur.execute(s)
    return DuckDBEngine(name, location, connection=cur, read_dialect=read_dialect)


@pytest.fixture(scope="module")
def same(db):
    return target_with(db, "same")


@pytest.fixture(scope="module")
def intdiv(db):
    return target_with(db, "intdiv", "SET SESSION integer_division = true")


@pytest.fixture(scope="module")
def rawduck(db):
    # Runs the Spark text as DuckDB SQL with DuckDB's own NULL placement (last both ways).
    return target_with(db, "rawduck", "SET SESSION default_null_order = 'nulls_last'", read_dialect="duckdb")


@pytest.fixture(scope="module")
def f32(db, source):
    t = source.clone_with_types("f32", decimal_as="REAL", schema="f32")
    return DuckDBEngine("f32", "f32", connection=db.cursor())


@pytest.fixture(scope="module")
def shuffled(db):
    db.execute("CREATE SCHEMA IF NOT EXISTS shuffled")
    db.execute("SELECT setseed(0.42)")
    db.execute("CREATE OR REPLACE TABLE shuffled.item AS SELECT * FROM main.item ORDER BY random()")
    return DuckDBEngine("shuffled", "shuffled", connection=db.cursor())


def q(sql, qid="custom"):
    return Query(qid, sql)


# ----------------------------------------------------------------- move


@pytest.mark.parametrize("qid", ["q3", "q7", "q19", "q42", "q52", "q55", "q96"])
def test_same_engine_moves(source, same, qid):
    j = Judge(source, same).judge(get(qid))
    assert j.verdict == "move", j.note
    assert j.comparison["status"] == "identical"
    assert [s.name for s in j.steps] == ["source", "target"]
    assert j.steps[0].fingerprint == j.steps[1].fingerprint


def test_judgement_records_sql_and_tables(source, same):
    j = Judge(source, same).judge(get("q42"))
    assert j.tables == ["date_dim", "item", "store_sales"]
    assert "main.store_sales" in j.source_sql and "main.store_sales" in j.target_sql
    d = j.as_dict()
    assert d["verdict"] == "move" and isinstance(d["steps"], list)
    assert d["finished_at"] >= d["started_at"]


# ----------------------------------------------------------------- integer division

DIV = "SELECT d_moy, CAST(d_moy AS INT) / CAST(2 AS INT) AS half_month FROM date_dim WHERE d_year = 2000 GROUP BY d_moy ORDER BY d_moy"


def test_integer_division_is_proven_and_rewritten(source, intdiv):
    j = Judge(source, intdiv).judge(q(DIV))
    assert j.verdict == "rewrite", j.note
    assert j.cause == "integer_division"
    assert "CAST(" in j.rewritten_sql and "AS DOUBLE)" in j.rewritten_sql
    assert j.comparison["status"] == "different"
    assert j.final_comparison["status"] == "identical"
    assert j.comparison["mismatched_columns"] == ["half_month"]


def test_integer_division_sample_shows_values(source, intdiv):
    j = Judge(source, intdiv).judge(q(DIV))
    pair = j.sample["paired"][0]
    assert pair["columns"] == ["half_month"]
    assert float(pair["source"][1]) != float(pair["target"][1])


def test_integer_division_on_tpcds_q90_style_ratio(source, intdiv):
    sql = (
        "SELECT CAST(amc AS INT) / CAST(pmc AS INT) AS ratio FROM "
        "(SELECT count(*) amc FROM web_sales WHERE ws_quantity > 50) a, "
        "(SELECT count(*) pmc FROM web_sales WHERE ws_quantity <= 50) b"
    )
    j = Judge(source, intdiv).judge(q(sql))
    assert j.verdict == "rewrite" and j.cause == "integer_division"


# ----------------------------------------------------------------- NULL ordering

NULLS = "SELECT ca_state, count(*) AS n FROM customer_address GROUP BY ca_state ORDER BY ca_state LIMIT 5"


def test_null_ordering_is_proven(source, rawduck):
    j = Judge(source, rawduck).judge(q(NULLS))
    assert j.verdict == "rewrite", (j.note, [s.as_dict() for s in j.steps])
    assert j.cause == "null_ordering"
    assert "NULLS FIRST" in j.rewritten_sql


def test_null_ordering_without_limit_is_order_only(source, rawduck):
    sql = "SELECT ca_state, count(*) AS n FROM customer_address GROUP BY ca_state ORDER BY ca_state"
    j = Judge(source, rawduck).judge(q(sql))
    assert j.comparison["status"] == "order_only"
    assert j.verdict == "rewrite" and j.cause == "null_ordering"


# ----------------------------------------------------------------- type drift


@pytest.fixture(scope="module")
def f32_alignment(source, f32):
    return {t: align_table(source, f32, t) for t in ("store_sales", "store", "date_dim", "item")}


SUM = "SELECT ss_ticket_number, ss_item_sk, ss_net_paid FROM store_sales WHERE ss_net_paid > 1000 ORDER BY ss_ticket_number, ss_item_sk LIMIT 50"
AGG = "SELECT ss_store_sk, SUM(ss_net_paid) AS paid FROM store_sales WHERE ss_store_sk IS NOT NULL GROUP BY ss_store_sk ORDER BY ss_store_sk"


def test_single_precision_drift_is_proven_by_casting_back(source, f32, f32_alignment):
    j = Judge(source, f32, alignment=f32_alignment).judge(q(SUM))
    assert j.comparison["status"] == "different", j.comparison
    assert j.comparison["type_mismatches"]
    assert j.verdict == "rewrite", j.note
    assert j.cause == "type_drift"
    assert "CAST(ss_net_paid AS DECIMAL(7, 2))" in j.rewritten_sql


def test_without_alignment_the_drift_cannot_be_named(source, f32):
    j = Judge(source, f32).judge(q(SUM))
    assert j.verdict == "keep"
    assert j.cause == "unexplained"


def test_single_precision_sums_are_float_noise_not_hidden(source, f32, f32_alignment):
    j = Judge(source, f32, alignment=f32_alignment).judge(q(AGG))
    assert j.verdict == "move_note" and j.cause == "float_noise"
    assert 0 < j.comparison["max_rel_gap"] <= 1e-9


def test_judge_drift_map_from_alignment(source, f32, f32_alignment):
    drift = Judge(source, f32, alignment=f32_alignment).drift_for(["store_sales"])
    assert drift["ss_net_paid"] == "decimal(7,2)"
    assert "ss_item_sk" not in drift


# ----------------------------------------------------------------- ties at a LIMIT

TIE = "SELECT i_category, i_item_id FROM item WHERE i_category IS NOT NULL ORDER BY i_category LIMIT 8"


def test_tie_at_limit_is_proven_not_assumed(source, shuffled):
    j = Judge(source, shuffled).judge(q(TIE))
    if j.verdict == "move":
        pytest.skip("this generator seed happened to keep the tied rows in the same order")
    assert j.verdict == "move_note", j.note
    assert j.cause == "tie_at_limit"
    names = [s.name for s in j.steps]
    assert "probe: source without LIMIT" in names and "probe: target with a total order" in names


# ----------------------------------------------------------------- blocked


def test_blocked_when_input_not_aligned(db, source):
    db.execute("CREATE SCHEMA IF NOT EXISTS short")
    db.execute("CREATE OR REPLACE TABLE short.reason AS SELECT * FROM main.reason WHERE r_reason_sk > 1")
    for t in ("store_sales", "store_returns", "item", "date_dim", "customer", "store"):
        db.execute(f"CREATE OR REPLACE VIEW short.{t} AS SELECT * FROM main.{t}")
    tgt = DuckDBEngine("short", "short", connection=db.cursor())
    al = {"reason": align_table(source, tgt, "reason")}
    assert al["reason"].status == "not_aligned"
    j = Judge(source, tgt, alignment=al).judge(get("q93"))
    assert j.verdict == "blocked" and j.cause == "input_not_aligned"
    assert j.steps == []


def test_blocked_when_source_cannot_run(source, same):
    j = Judge(source, same).judge(get("q30"))
    assert j.verdict == "blocked" and j.cause == "source_error"
    assert "c_last_review_date" in j.note


# ----------------------------------------------------------------- target error


def test_target_error_kept_with_message(db, source):
    db.execute("CREATE SCHEMA IF NOT EXISTS broken")
    db.execute("CREATE OR REPLACE VIEW broken.item AS SELECT i_item_sk FROM main.item")
    tgt = DuckDBEngine("broken", "broken", connection=db.cursor())
    j = Judge(source, tgt).judge(q("SELECT i_brand FROM item ORDER BY i_brand LIMIT 3"))
    assert j.verdict == "keep" and j.cause == "target_error"
    assert "i_brand" in j.note


# ----------------------------------------------------------------- advisor


class FixedAdvisor:
    def __init__(self, sql):
        self.sql, self.calls = sql, []

    def propose(self, **kw):
        self.calls.append(kw)
        return self.sql


class BrokenAdvisor:
    def propose(self, **kw):
        raise RuntimeError("model offline")


def test_advisor_rewrite_is_checked_by_execution(source, intdiv):
    good = "SELECT d_moy, CAST(d_moy AS DOUBLE) / 2 AS half_month FROM main.date_dim WHERE d_year = 2000 GROUP BY d_moy ORDER BY d_moy"
    adv = FixedAdvisor(good)
    j = Judge(source, intdiv, advisor=adv).judge(q(DIV.replace("/", "DIV").replace("DIV", "/")))
    # The deterministic rewrite wins before the advisor is asked.
    assert j.cause == "integer_division" and adv.calls == []


def test_advisor_used_when_rules_fail(source, f32):
    good = "SELECT ss_ticket_number, ss_item_sk, CAST(ss_net_paid AS DECIMAL(7,2)) AS ss_net_paid FROM f32.store_sales WHERE ss_net_paid > 1000 ORDER BY ss_ticket_number, ss_item_sk LIMIT 50"
    adv = FixedAdvisor(good)
    j = Judge(source, f32, advisor=adv).judge(q(SUM))
    assert j.verdict == "rewrite" and j.cause == "model_rewrite"
    assert adv.calls and adv.calls[0]["query_id"] == "custom"
    assert "checked by execution" in j.note


def test_wrong_advisor_rewrite_is_refused(source, f32):
    adv = FixedAdvisor("SELECT 1 AS ss_store_sk, 2 AS paid")
    j = Judge(source, f32, advisor=adv).judge(q(SUM))
    assert j.verdict == "keep"
    step = next(s for s in j.steps if s.name == "rewrite: model proposal")
    assert step.detail.endswith("-> schema_mismatch")


def test_offline_advisor_does_not_break_the_judge(source, f32):
    j = Judge(source, f32, advisor=BrokenAdvisor()).judge(q(SUM))
    assert j.verdict == "keep"
    assert any(s.name == "advisor" and "model offline" in s.detail for s in j.steps)


# ----------------------------------------------------------------- memory


class ListMemory:
    def __init__(self, recalled=()):
        self.recalled, self.saved = list(recalled), []

    def recall(self, feats):
        return self.recalled

    def remember(self, feats, rewrite, query_id):
        self.saved.append((tuple(feats), rewrite, query_id))


def test_proven_fix_is_remembered(source, intdiv):
    mem = ListMemory()
    Judge(source, intdiv, memory=mem).judge(q(DIV, "div1"))
    assert mem.saved and mem.saved[0][1] == "cast_divisions_to_double" and mem.saved[0][2] == "div1"
    assert "has_division" in mem.saved[0][0]


def test_recalled_fix_is_tried_first(source, intdiv):
    mem = ListMemory(["cast_divisions_to_double"])
    j = Judge(source, intdiv, memory=mem).judge(q(DIV))
    rewrites = [s.name for s in j.steps if s.name.startswith("rewrite")]
    assert rewrites[0] == "rewrite: cast_divisions_to_double"


def test_unknown_recalled_names_are_ignored(source, intdiv):
    j = Judge(source, intdiv, memory=ListMemory(["drop_table", "x+y"])).judge(q(DIV))
    assert j.cause == "integer_division"


# ----------------------------------------------------------------- features


def test_features_are_plain_facts():
    f = features(None, "select a / b", "UNRESOLVED_COLUMN: x", {"a": "decimal(7,2)"})
    assert f == ["target_error", "error:unresolved_column", "has_division", "reads_retyped_columns"]


@pytest.mark.parametrize("sql,expected", [("SELECT a / b FROM t", True), ("SELECT a FROM t WHERE s = 'N/A'", False), ("SELECT 1", False), ("not sql at all (", False)])
def test_has_division_ignores_string_literals(sql, expected):
    from nirvana.judge import has_division

    assert has_division(sql) is expected


def test_q41_is_not_flagged_as_dividing():
    from nirvana.judge import has_division

    assert has_division(get("q41").sql) is False
    assert has_division(get("q90").sql) is True


def test_truncated_identical_answer_is_not_a_plain_move(source, same):
    j = Judge(source, same, max_rows=5).judge(q("SELECT i_item_sk FROM item ORDER BY i_item_sk"))
    assert j.verdict == "move_note" and j.cause == "truncated"
    assert "first 5 rows" in j.note
