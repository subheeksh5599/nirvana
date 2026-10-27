"""Judge one query: run it on both engines, compare, and when the answers differ find a cause
that execution proves.

Every step is recorded (what ran, where, how many rows, which fingerprint, how long) so the
ledger and the UI can show the path to a verdict, not just the verdict. Verdicts:

* ``move``       - same answer on both engines
* ``move_note``  - same answer up to something the source itself does not fix: float noise
                    inside the stated bound, or which tied rows fill the last places of a LIMIT
                    (proven by re-running without the LIMIT)
* ``rewrite``    - the target gives the source's exact answer once the query is rewritten;
                    the rewrite and the cause it proves are recorded
* ``keep``       - no proven way to give the same answer; stays on the source
* ``blocked``    - an input table is not aligned, or the source itself fails the query
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import sqlglot
from sqlglot import exp

from . import rewrite as rw
from .align import TableAlignment
from .compare import Comparison, compare
from .engines import Engine
from .model import EngineError, ResultSet
from .workload import Query, has_top_level_order, qualify, tables_read, top_level_limit

CAUSE_OF = {
    "cast_drifted_columns": "type_drift",
    "cast_divisions_to_double": "integer_division",
    "explicit_null_order": "null_ordering",
    "transpile_from_source": "dialect",
    "apply_repairs": "proven_repair",
    "model": "model_rewrite",
}


class Advisor(Protocol):
    """Something that proposes a rewrite for an unexplained difference (the language model)."""

    def propose(self, *, query_id: str, target_sql: str, diff: dict[str, Any], target_error: str | None) -> str | None: ...


class Memory(Protocol):
    def recall(self, features: list[str]) -> list[str]: ...

    def remember(self, features: list[str], rewrite: str, query_id: str) -> None: ...


@dataclass
class Step:
    name: str
    engine: str
    ok: bool
    detail: str
    rows: int | None = None
    fingerprint: str | None = None
    ms: float = 0.0
    sql: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Judgement:
    query_id: str
    verdict: str
    cause: str | None
    note: str
    source_sql: str
    target_sql: str
    rewritten_sql: str | None = None
    comparison: dict[str, Any] | None = None
    final_comparison: dict[str, Any] | None = None
    steps: list[Step] = field(default_factory=list)
    source_ms: float = 0.0
    target_ms: float = 0.0
    tables: list[str] = field(default_factory=list)
    sample: dict[str, Any] | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "steps"}
        d["steps"] = [s.as_dict() for s in self.steps]
        return d


def _sample(c: Comparison) -> dict[str, Any]:
    def enc(row):
        return [None if t[0] == "null" else t[1] for t in row]

    return {
        "paired": [
            {"source": enc(p.source), "target": enc(p.target), "columns": [x.name for x in p.cells]}
            for p in c.paired[:5]
        ],
        "only_in_source": [enc(r) for r in c.unpaired_source[:5]],
        "only_in_target": [enc(r) for r in c.unpaired_target[:5]],
    }


def has_division(sql: str) -> bool:
    """True when the statement divides (a '/' inside a string literal such as 'N/A' does not count)."""
    try:
        return any(True for _ in sqlglot.parse_one(sql, read="spark").find_all(exp.Div))
    except Exception:
        return False


# What the judge writes to memory when a same-rows, different-order answer was not changed
# by an explicit NULL order: the next query with the same features skips that execution.
ORDER_TIES_PROOF = "proof:order_ties"


def features(cmp: Comparison | None, target_sql: str, target_error: str | None, drift: dict[str, str]) -> list[str]:
    """Plain facts about a difference, used to recall which rewrite fixed a similar one."""
    f = []
    if target_error:
        f.append("target_error")
        f.append("error:" + target_error.split(":", 1)[0][:40].strip().lower())
    if cmp is not None:
        f.append("status:" + cmp.status)
        if cmp.type_mismatches:
            f.append("output_type_changed")
        if cmp.source_rows != cmp.target_rows:
            f.append("row_count_changed")
    if has_division(target_sql):
        f.append("has_division")
    if drift:
        f.append("reads_retyped_columns")
    return f


class Judge:
    def __init__(
        self,
        source: Engine,
        target: Engine,
        *,
        alignment: dict[str, TableAlignment] | None = None,
        advisor: Advisor | None = None,
        memory: Memory | None = None,
        rel_tol: float = 1e-9,
        max_rows: int = 100_000,
        clock: Callable[[], float] = time.time,
        repairs: dict[str, str] | None = None,
    ) -> None:
        self.repairs = {k.lower(): v for k, v in (repairs or {}).items()}
        self.source, self.target = source, target
        self.alignment = alignment or {}
        self.advisor, self.memory = advisor, memory
        self.rel_tol, self.max_rows, self.clock = rel_tol, max_rows, clock

    # ------------------------------------------------------------------ helpers

    def _write_dialect(self, engine: Engine) -> str:
        return "databricks" if engine.dialect == "databricks" else "spark"

    def _run(self, engine: Engine, sql: str, name: str, steps: list[Step]) -> ResultSet | EngineError:
        try:
            rs = engine.run(sql, max_rows=self.max_rows)
        except EngineError as e:
            steps.append(Step(name, engine.name, False, e.message, sql=sql))
            return e
        from .canon import fingerprint, row_tokens

        fp = fingerprint((row_tokens(r, rs.columns) for r in rs.rows), ordered=False)
        detail = f"{rs.row_count} rows" + (" (truncated)" if rs.truncated else "")
        steps.append(Step(name, engine.name, True, detail, rs.row_count, fp, rs.elapsed_ms, sql))
        return rs

    def drift_for(self, tables: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for t in tables:
            a = self.alignment.get(t)
            if a is None:
                continue
            for ch in a.type_changes:
                out[ch["column"].lower()] = ch["source"]
        return out

    # ------------------------------------------------------------------ judge

    def judge(self, q: Query) -> Judgement:
        started = self.clock()
        tables = tables_read(q.sql)
        s_sql = qualify(q.sql, self.source.location, self._write_dialect(self.source))
        t_sql = qualify(q.sql, self.target.location, self._write_dialect(self.target))
        j = Judgement(q.id, "keep", None, "", s_sql, t_sql, tables=tables, started_at=started)

        blocked = [t for t in tables if t in self.alignment and not self.alignment[t].usable]
        if blocked:
            j.verdict, j.cause = "blocked", "input_not_aligned"
            j.note = "input tables differ between engines: " + ", ".join(blocked)
            j.finished_at = self.clock()
            return j

        steps = j.steps
        src = self._run(self.source, s_sql, "source", steps)
        if isinstance(src, EngineError):
            j.verdict, j.cause = "blocked", "source_error"
            j.note = f"the source cannot run this query: {src.message}"
            j.finished_at = self.clock()
            return j
        j.source_ms = src.elapsed_ms
        ordered = has_top_level_order(q.sql)
        drift = self.drift_for(tables)

        tgt = self._run(self.target, t_sql, "target", steps)
        target_error = tgt.message if isinstance(tgt, EngineError) else None
        cmp: Comparison | None = None
        if isinstance(tgt, ResultSet):
            j.target_ms = tgt.elapsed_ms
            cmp = compare(src, tgt, ordered=ordered, rel_tol=self.rel_tol)
            j.comparison = cmp.summary()
            if cmp.status != "identical":
                j.sample = _sample(cmp)
            if cmp.status == "identical" and (src.truncated or tgt.truncated):
                return self._done(j, "move_note", "truncated", f"the first {self.max_rows} rows are identical; the rest were not fetched")
            if cmp.status == "identical":
                return self._done(j, "move", None, "same answer on both engines")
            if cmp.status == "float_noise":
                return self._done(j, "move_note", "float_noise", f"floating-point differences only, largest relative gap {cmp.max_rel_gap:.2e}")
            if cmp.status == "order_only":
                feats = features(cmp, t_sql, None, drift)
                knows = getattr(self.memory, "knows", None)
                if knows is not None and feats and knows(feats, ORDER_TIES_PROOF):
                    steps.append(Step("memory", "h-mem", True, "recalled: an explicit NULL order did not change this kind of difference before, so it was not re-run"))
                    return self._done(j, "move_note", "order_ties", "same rows; the order differs only where the ORDER BY does not decide it (recalled from memory)")
                proven = self._try_rewrites(j, src, t_sql, ordered, ["explicit_null_order"], drift, steps, [])
                if proven:
                    return proven
                if self.memory is not None and feats:
                    self.memory.remember(feats, ORDER_TIES_PROOF, j.query_id)
                return self._done(j, "move_note", "order_ties", "same rows; the order differs only where the ORDER BY does not decide it")

        feats = features(cmp, t_sql, target_error, drift)
        order = self._candidate_order(cmp, t_sql, target_error, drift, feats)
        proven = self._try_rewrites(j, src, t_sql, ordered, order, drift, steps, feats)
        if proven:
            return proven
        limit = top_level_limit(q.sql)
        if cmp is not None and ordered and limit is not None and cmp.source_rows == cmp.target_rows:
            if self._probe_tie(s_sql, t_sql, steps):
                return self._done(
                    j, "move_note", "tie_at_limit",
                    f"without the LIMIT {limit} both engines return the same rows, and with every output column added to the ORDER BY they return the same first {limit}; the difference is which tied rows the source happens to pick",
                )
        if self.advisor is not None:
            proven = self._try_advisor(j, src, t_sql, ordered, cmp, target_error, steps)
            if proven:
                return proven
        if target_error:
            return self._done(j, "keep", "target_error", f"the target cannot run this query: {target_error}")
        return self._done(j, "keep", "unexplained", "the answers differ and no rewrite reproduced the source's answer")

    def _done(self, j: Judgement, verdict: str, cause: str | None, note: str) -> Judgement:
        j.verdict, j.cause, j.note = verdict, cause, note
        j.finished_at = self.clock()
        return j

    def _probe_tie(self, s_sql: str, t_sql: str, steps: list[Step]) -> bool:
        """Two probes, both required: (1) without the LIMIT the engines return the same
        multiset; (2) with every output column appended to the ORDER BY (a total order) they
        return the same rows in the same order. Together they show the rows differ only in
        how ties at the cut were broken."""
        s_nolimit, t_nolimit = rw.drop_top_limit(s_sql), rw.drop_top_limit(t_sql)
        if not s_nolimit or not t_nolimit:
            return False
        a = self._run(self.source, s_nolimit, "probe: source without LIMIT", steps)
        b = self._run(self.target, t_nolimit, "probe: target without LIMIT", steps)
        if isinstance(a, EngineError) or isinstance(b, EngineError) or a.truncated or b.truncated:
            return False
        if compare(a, b, ordered=False, rel_tol=self.rel_tol).status not in ("identical", "float_noise"):
            return False
        s_total, t_total = rw.total_order(s_sql, {}), rw.total_order(t_sql, {})
        if not s_total or not t_total:
            return False
        c = self._run(self.source, s_total, "probe: source with a total order", steps)
        d = self._run(self.target, t_total, "probe: target with a total order", steps)
        if isinstance(c, EngineError) or isinstance(d, EngineError):
            return False
        return compare(c, d, ordered=True, rel_tol=self.rel_tol).status in ("identical", "float_noise")

    def _candidate_order(self, cmp: Comparison | None, t_sql: str, target_error: str | None, drift: dict[str, str], feats: list[str]) -> list[str]:
        base: list[str] = []
        if self.repairs and any(c.name.lower() in self.repairs for c in sqlglot.parse_one(t_sql, read="spark").find_all(exp.Column)):
            base.append("apply_repairs")
        if target_error:
            base.append("transpile_from_source")
        if drift:
            base.append("cast_drifted_columns")
        if has_division(t_sql):
            base.append("cast_divisions_to_double")
        if cmp is not None and cmp.ordered:
            base.append("explicit_null_order")
        if self.memory is not None:
            recalled = [r for r in self.memory.recall(feats) if all(p in rw.CATALOGUE for p in r.split("+"))]
            base = recalled + [b for b in base if b not in recalled]
        return base

    def _try_rewrites(self, j, src: ResultSet, t_sql: str, ordered: bool, names: list[str], drift: dict[str, str], steps: list[Step], feats: list[str]):
        ctx = {
            "repairs": self.repairs,
            "drift": drift,
            "source_dialect": self._write_dialect(self.source),
            "target_dialect": self._write_dialect(self.target),
            "null_order": self.source.null_order,
        }
        tried: set[str] = set()
        # Single rewrites first, then the drift fix combined with each other rewrite.
        plans = [n.split("+") for n in names]
        if "cast_drifted_columns" in names:
            plans += [["cast_drifted_columns", n] for n in names if n != "cast_drifted_columns" and "+" not in n]
        for plan in plans:
            try:
                new_sql = rw.chain(t_sql, plan, ctx)
            except Exception as e:  # a rewrite that cannot parse its input is just not applicable
                steps.append(Step("rewrite: " + "+".join(plan), self.target.name, False, f"not applicable: {e}"))
                continue
            if not new_sql or new_sql in tried:
                continue
            tried.add(new_sql)
            out = self._run(self.target, new_sql, "rewrite: " + "+".join(plan), steps)
            if isinstance(out, EngineError):
                continue
            c = compare(src, out, ordered=ordered, rel_tol=self.rel_tol)
            steps[-1].detail += f" -> {c.status}"
            if c.status == "identical":
                j.rewritten_sql = new_sql
                j.final_comparison = c.summary()
                cause = "+".join(CAUSE_OF.get(p, p) for p in plan)
                if self.memory is not None and feats:
                    self.memory.remember(feats, "+".join(plan), j.query_id)
                return self._done(j, "rewrite", cause, "the target returns the source's exact answer after: " + ", ".join(rw.CATALOGUE[p].describe for p in plan))
        if self.memory is not None and feats and hasattr(self.memory, "forget_failed"):
            self.memory.forget_failed()
        return None

    def _try_advisor(self, j, src: ResultSet, t_sql: str, ordered: bool, cmp: Comparison | None, target_error: str | None, steps: list[Step]):
        assert self.advisor is not None
        try:
            proposal = self.advisor.propose(query_id=j.query_id, target_sql=t_sql, diff=j.comparison or {}, target_error=target_error)
        except Exception as e:
            steps.append(Step("advisor", "model", False, f"no proposal: {e}"))
            return None
        if not proposal or proposal.strip() == t_sql.strip():
            steps.append(Step("advisor", "model", False, "no proposal"))
            return None
        steps.append(Step("advisor", "model", True, "proposed a rewrite", sql=proposal))
        out = self._run(self.target, proposal, "rewrite: model proposal", steps)
        if isinstance(out, EngineError):
            return None
        c = compare(src, out, ordered=ordered, rel_tol=self.rel_tol)
        steps[-1].detail += f" -> {c.status}"
        if c.status != "identical":
            return None
        j.rewritten_sql = proposal
        j.final_comparison = c.summary()
        return self._done(j, "rewrite", "model_rewrite", "the model's rewrite returns the source's exact answer on the target (checked by execution, not by the model)")

