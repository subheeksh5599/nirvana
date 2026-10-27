"""The investigating agent: the model picks tools, the engines answer, only rows prove."""

import json

import duckdb
import pytest

from nirvana.engines.duck import DuckDBEngine
from nirvana.investigate import (
    Agent, Investigation, InvestigationStore, differs_sql, parse_action, safe_expression,
)
from nirvana.model import Column, EngineError, ResultSet


# ----------------------------------------------------------------- parsing the model's reply


def test_parse_plain_json():
    a = parse_action('{"tool": "compare_column", "args": {"column": "ca_zip"}, "why": "check"}')
    assert a == {"tool": "compare_column", "args": {"column": "ca_zip"}, "why": "check"}


def test_parse_json_with_prose_and_think_blocks():
    a = parse_action('<think>long thoughts {"tool":"x"}</think>Sure: {"tool":"profile_column","args":{"column":"a"},"why":"w"} done.')
    assert a["tool"] == "profile_column" and a["args"] == {"column": "a"}


def test_parse_sql_block_after_json():
    a = parse_action('{"tool":"try_rewrite","why":"cast"}\n```sql\nSELECT 1\n```')
    assert a["args"]["sql"] == "SELECT 1"


def test_parse_bare_sql_block_is_a_rewrite():
    a = parse_action("Try this:\n```sql\nWITH a AS (SELECT 1) SELECT * FROM a\n```")
    assert a["tool"] == "try_rewrite" and a["args"]["sql"].startswith("WITH")


@pytest.mark.parametrize("bad", ["", "no json here", '{"args": {}}', "[1, 2]"])
def test_parse_refuses_replies_without_a_tool(bad):
    with pytest.raises(ValueError):
        parse_action(bad)


def test_parse_missing_args_is_empty():
    assert parse_action('{"tool":"finish"}')["args"] == {}


# ----------------------------------------------------------------- repair expressions


def test_safe_expression_binds_x_to_the_target_column():
    assert safe_expression("lpad(cast(x as string), 5, '0')", "ca_zip") == "LPAD(CAST(c.ca_zip AS STRING), 5, '0')"


@pytest.mark.parametrize("bad", [
    "x; DROP TABLE t", "(SELECT max(a) FROM t)", "y + 1", "x + ca_city", "", "x" * 500, "not ( valid",
])
def test_safe_expression_refuses(bad):
    with pytest.raises(ValueError):
        safe_expression(bad, "ca_zip")


def test_differs_numbers_as_numbers_and_text_as_text():
    assert "ABS(CAST(s.a AS DOUBLE) - CAST(c.a AS DOUBLE))" in differs_sql("a", "decimal", "float")
    assert differs_sql("a", "string", "integer") == "NOT (CAST(s.a AS STRING) <=> CAST(c.a AS STRING))"
    assert differs_sql("a", "string", "integer", "LPAD(c.a, 5, '0')") == "NOT (CAST(s.a AS STRING) <=> CAST(LPAD(c.a, 5, '0') AS STRING))"


# ----------------------------------------------------------------- the loop, against scripted engines


class Script:
    """A model that replies from a list, recording the prompts it was shown."""

    model = "scripted"

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, system, user, **kw):
        self.prompts.append(user)
        if not self.replies:
            return '{"tool":"finish","args":{"summary":"out of replies"}}'
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


class Fed:
    """A Zetaris stand-in answering the agent's federated statements from two row lists."""

    name, dialect, null_order = "zetaris", "spark", "spark"

    def __init__(self, src_rows, tgt_rows):
        self.src, self.tgt, self.sql = src_rows, tgt_rows, []

    def _rs(self, cols, rows):
        return ResultSet.build("zetaris", "", [Column(c, "string") for c in cols], rows)

    def run(self, sql, *, max_rows=100_000):
        self.sql.append(sql)
        if sql.endswith("LIMIT 0"):
            return ResultSet.build("zetaris", sql, [Column("k", "integer"), Column("zip", "integer")], [])
        if sql.startswith("CREATE DATASOURCE VIEW"):
            return self._rs([], [])
        if "LPAD" in sql:
            pairs = [(k, z, str(t).zfill(5)) for (k, z), (_, t) in zip(self.src, self.tgt)]
        else:
            pairs = [(k, z, str(t)) for (k, z), (_, t) in zip(self.src, self.tgt)]
        bad = [p for p in pairs if p[1] != p[2]]
        if sql.startswith("SELECT COUNT(*), COALESCE"):
            return self._rs(["n", "bad"], [(len(pairs), len(bad))])
        if sql.startswith("SELECT s."):
            return self._rs(["k", "s", "t"], bad[:5])
        if sql.startswith("SELECT COUNT(*), COUNT("):
            return self._rs(["a"] * 6, [(len(self.tgt), len(self.tgt), "601", "94110", 3, 5)])
        raise EngineError("zetaris", f"unexpected {sql[:60]}")


class Src:
    name, dialect, null_order = "databricks", "databricks", "spark"

    def __init__(self, rows):
        self.rows = rows

    def run(self, sql, *, max_rows=100_000):
        if sql.endswith("LIMIT 0"):
            return ResultSet.build("databricks", sql, [Column("k", "integer"), Column("zip", "string")], [])
        if sql.startswith("SELECT COUNT(*) FROM"):
            return ResultSet.build("databricks", sql, [Column("n", "bigint")], [(len(self.rows),)])
        return ResultSet.build("databricks", sql, [Column("a", "string")] * 6, [(len(self.rows), len(self.rows), "00601", "94110", 5, 5)])


class Mem:
    def __init__(self, recalled=()):
        self.recalled, self.saved = list(recalled), []

    def recall_findings(self, text):
        return self.recalled

    def remember_finding(self, text, tags):
        self.saved.append((text, tags))


SRC = [(1, "00601"), (2, "02134"), (3, "94110")]
TGT = [(1, 601), (2, 2134), (3, 94110)]
ALIGN = {"differences": [{"measure": "zip.min", "source": "00601", "target": "601"}],
         "type_changes": [{"column": "zip", "source": "string", "target": "integer"}]}


def agent(model, mem=None, tgt=TGT):
    return Agent(model, Src(SRC), Fed(SRC, tgt), source_location="ws.db", memory=mem)


def test_the_agent_finds_and_proves_a_repair():
    model = Script(
        {"tool": "compare_column", "args": {"column": "zip"}, "why": "look"},
        {"tool": "verify_repair", "args": {"column": "zip", "expression": "lpad(cast(x as string), 5, '0')"}, "why": "pad"},
    )
    mem = Mem()
    inv = agent(model, mem).investigate_table("addr", "CSV", ALIGN)
    assert inv.status == "done" and inv.cleared
    assert inv.steps[0].result["differing_rows"] == 2
    assert inv.steps[0].result["examples"][0] == {"key": 1, "databricks": "00601", "zetaris": "601"}
    assert inv.steps[1].result["differing_rows"] == 0 and inv.steps[1].result["proven"]
    assert [(f.column, f.kind) for f in inv.findings] == [("zip", "repair")]
    assert mem.saved and "repair:lpad(cast(x as string), 5, '0')" in mem.saved[0][1]
    assert "change:string->integer" in mem.saved[0][1]


def test_a_wrong_repair_is_not_a_finding():
    model = Script({"tool": "verify_repair", "args": {"column": "zip", "expression": "cast(x as string)"}, "why": "guess"})
    inv = agent(model).investigate_table("addr", "CSV", ALIGN)
    assert not inv.cleared and inv.findings == []
    assert inv.steps[0].result["differing_rows"] == 2


def test_the_model_saying_finish_proves_nothing():
    inv = agent(Script({"tool": "finish", "args": {"summary": "all fine, trust me"}})).investigate_table("addr", "CSV", ALIGN)
    assert inv.status == "done" and not inv.cleared and inv.summary == "all fine, trust me"


def test_same_values_clear_the_column():
    same = [(1, "00601"), (2, "02134"), (3, "94110")]
    inv = Agent(Script({"tool": "compare_column", "args": {"column": "zip"}, "why": "x"}), Src(SRC), Fed(SRC, same),
                source_location="ws.db").investigate_table("addr", "CSV", ALIGN)
    assert inv.cleared and inv.findings[0].kind == "same_values"


def test_bad_replies_and_bad_tools_are_steps_not_crashes():
    model = Script("gibberish", RuntimeError("502"), {"tool": "teleport", "args": {"column": "zip"}},
                   {"tool": "compare_column", "args": {"column": "nope"}}, {"tool": "finish"})
    inv = agent(model).investigate_table("addr", "CSV", ALIGN)
    assert inv.status == "done"
    assert [s.ok for s in inv.steps[:4]] == [False, False, False, False]
    assert "no column nope" in inv.steps[3].result["error"]


def test_repeated_calls_are_refused():
    call = {"tool": "compare_column", "args": {"column": "zip"}, "why": "x"}
    inv = agent(Script(call, call, {"tool": "finish"})).investigate_table("addr", "CSV", ALIGN)
    assert "already made" in inv.steps[1].result["error"]


def test_steps_are_capped():
    model = Script(*[{"tool": "profile_column", "args": {"column": "zip"}, "why": str(i)} for i in range(20)])
    a = agent(model)
    a.max_steps = 3
    inv = a.investigate_table("addr", "CSV", ALIGN)
    assert len(inv.steps) == 3


def test_memory_is_shown_to_the_model():
    model = Script({"tool": "finish"})
    agent(model, Mem([{"content": "zip lost its leading zeros; lpad repaired it"}])).investigate_table("addr", "CSV", ALIGN)
    assert "Memory (Meterless H-MEM) recalls" in model.prompts[0] and "lpad repaired it" in model.prompts[0]


def test_the_loop_stops_once_everything_is_proven():
    model = Script({"tool": "verify_repair", "args": {"column": "zip", "expression": "lpad(cast(x as string), 5, '0')"}},
                   {"tool": "profile_column", "args": {"column": "zip"}})
    inv = agent(model).investigate_table("addr", "CSV", ALIGN)
    assert inv.cleared and len(inv.steps) == 1 and len(model.replies) == 1


def test_query_tools_refuse_table_tools():
    inv = Agent(Script({"tool": "compare_column", "args": {"column": "a"}}, {"tool": "finish"}), Src(SRC), Fed(SRC, TGT),
                source_location="x").investigate_query("q1", "CSV", {"source_sql": "SELECT 1", "target_sql": "SELECT 1"})
    assert "for a query use try_rewrite" in inv.steps[0].result["error"]


# ----------------------------------------------------------------- query cases on real DuckDB engines


@pytest.fixture(scope="module")
def pair():
    c = duckdb.connect()
    c.execute("CREATE SCHEMA s; CREATE SCHEMA t")
    c.execute("CREATE TABLE s.sales AS SELECT * FROM (VALUES ('a', 1.10::DECIMAL(7,2)), ('a', 2.20::DECIMAL(7,2)), ('b', 0.30::DECIMAL(7,2))) v(k, p)")
    c.execute("CREATE TABLE t.sales AS SELECT k, p::DOUBLE AS p FROM s.sales")
    return DuckDBEngine("src", "s", connection=c.cursor()), DuckDBEngine("tgt", "t", connection=c.cursor())


JUDGED = {"source_sql": "SELECT k, SUM(p) * 3 AS total FROM s.sales GROUP BY k ORDER BY k",
          "target_sql": "SELECT k, SUM(p) * 3 AS total FROM t.sales GROUP BY k ORDER BY k",
          "comparison": {"status": "different", "mismatched_columns": ["total"]}, "drift": {"p": "decimal(7,2)"}}


def test_round_output_is_proven_on_the_rows(pair):
    src, tgt = pair
    inv = Agent(Script({"tool": "round_output", "args": {"columns": ["total"], "digits": 2}, "why": "precision"}), src, tgt,
                source_location="s").investigate_query("qx", "t", JUDGED)
    assert inv.steps[0].result["status"] == "identical", inv.steps[0].result
    assert inv.cleared and inv.findings[0].kind == "rewrite" and "ROUND" in inv.findings[0].expression


def test_apply_rule_casts_the_drifted_columns(pair):
    src, tgt = pair
    inv = Agent(Script({"tool": "apply_rule", "args": {"rule": "cast_drifted_columns"}}), src, tgt,
                source_location="s").investigate_query("qx", "t", JUDGED)
    assert inv.steps[0].result["status"] == "identical" and inv.cleared


def test_a_model_written_rewrite_with_wrong_rows_is_refused(pair):
    src, tgt = pair
    reply = '{"tool":"try_rewrite","why":"drop b"}\n```sql\nSELECT k, SUM(p) * 3 AS total FROM t.sales WHERE k = \'a\' GROUP BY k ORDER BY k\n```'
    inv = Agent(Script(reply, {"tool": "finish"}), src, tgt, source_location="s").investigate_query("qx", "t", JUDGED)
    assert inv.steps[0].result["status"] != "identical" and not inv.cleared


@pytest.mark.parametrize("args,err", [({"rule": "rm_rf"}, "unknown rule"), ({"columns": [], "digits": 2}, "at least one"),
                                      ({"columns": ["nope"], "digits": 2}, "none of"), ({"columns": ["total"], "digits": 99}, "between")])
def test_query_tool_errors(pair, args, err):
    src, tgt = pair
    tool = "apply_rule" if "rule" in args else "round_output"
    inv = Agent(Script({"tool": tool, "args": args}, {"tool": "finish"}), src, tgt, source_location="s").investigate_query("qx", "t", JUDGED)
    assert err in inv.steps[0].result["error"]


# ----------------------------------------------------------------- store


def test_store_keeps_live_and_finished(tmp_path):
    st = InvestigationStore(tmp_path / "i.jsonl")
    a = Investigation("a1", "table", "t", "C", started_at=1.0)
    st.put(a)
    assert st.get("a1")["status"] == "running" and not (tmp_path / "i.jsonl").exists()
    a.status = "done"
    st.put(a)
    assert InvestigationStore(tmp_path / "i.jsonl").get("a1")["status"] == "done"
    st.put(Investigation("b2", "query", "q1", "C", status="done", started_at=2.0))
    assert [x["id"] for x in st.all()] == ["b2", "a1"]


TYPED = {**JUDGED, "comparison": {"status": "different", "mismatched_columns": ["total"],
                                  "type_mismatches": [{"name": "total", "source": "decimal(38,2)", "target": "double"}]}}


def test_cast_output_reads_outputs_at_the_declared_type(pair):
    src, tgt = pair
    inv = Agent(Script({"tool": "cast_output", "args": {"columns": []}}), src, tgt, source_location="s").investigate_query("qx", "t", TYPED)
    assert inv.steps[0].result["status"] == "identical" and inv.cleared
    assert "CAST(ROUND(" in inv.findings[0].expression and "DECIMAL(38, 2)" in inv.findings[0].expression


def test_cast_output_refuses_columns_without_a_declared_type(pair):
    src, tgt = pair
    inv = Agent(Script({"tool": "cast_output", "args": {"columns": ["k"]}}, {"tool": "finish"}), src, tgt,
                source_location="s").investigate_query("qx", "t", TYPED)
    assert "no declared source type" in inv.steps[0].result["error"]


class Flaky(Script):
    def __init__(self, *replies):
        super().__init__(*replies)
        self.model, self.used = "main", []

    def complete(self, system, user, **kw):
        self.used.append(self.model)
        if self.model == "main":
            raise RuntimeError("502")
        return super().complete(system, user)


def test_a_failed_model_call_falls_back_once():
    m = Flaky({"tool": "finish", "args": {"summary": "ok"}})
    a = agent(m)
    a.fallback_model = "spare"
    inv = a.investigate_table("addr", "CSV", ALIGN)
    assert inv.summary == "ok" and m.used == ["main", "spare"] and m.model == "main"


def test_a_rate_limited_model_stops_the_investigation():
    inv = agent(Script(RuntimeError("429: Rate limit exceeded: free-models-per-day"), {"tool": "finish"})).investigate_table("addr", "CSV", ALIGN)
    assert len(inv.steps) == 1 and inv.summary.startswith("stopped: the model route is unavailable")


def test_three_model_failures_in_a_row_stop_it():
    inv = agent(Script(RuntimeError("502"), RuntimeError("502"), RuntimeError("502"), {"tool": "finish"})).investigate_table("addr", "CSV", ALIGN)
    assert len(inv.steps) == 3 and inv.summary.startswith("stopped")
