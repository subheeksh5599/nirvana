"""The investigating agent.

When alignment blocks a table, or a query ends in ``keep``, Nirvana hands the case to an
agent. The model (Nemotron, through Meterless Nightshift) chooses the next step from a fixed
set of tools; Nirvana runs that step on Databricks and Zetaris and gives the model the rows
that came back; the model chooses again. The model decides *what to look at*. It never
decides what is true: a column is cleared only by a comparison that found no differing row,
a repair only by a federated count of zero mismatches, and a query only by a rewrite whose
rows equal the source's. Every proven finding is written to Meterless H-MEM, and the next
investigation starts from what H-MEM recalls.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import sqlglot
from sqlglot import exp

from .canon import fingerprint, row_tokens
from .compare import compare
from .engines.base import Engine
from .federate import DATABRICKS
from .guard import build_guard, ident
from .model import Column, EngineError, ResultSet, type_family

MAX_STEPS = 8
NUMERIC = ("integer", "float", "decimal")

SYSTEM = """You are the investigating agent of Nirvana, which proves whether data and queries can move from Databricks (the source) to Zetaris (the target).
You do not decide what is true. You choose the next tool to run; Nirvana runs it on the real engines and shows you the result.
Reply with exactly one JSON object and nothing else:
{"tool": "<name>", "args": {...}, "why": "<one short sentence>"}

Tools:
- profile_column {"column": c}: type, row count, NULLs, min/max and text lengths of c on both engines.
- compare_column {"column": c}: joins both copies on the table key in one federated Zetaris query and counts rows whose value of c differs, with examples. Numbers are compared as numbers, other types as text.
- verify_repair {"column": c, "expression": e}: e is a Spark SQL expression over x, the target's value of c (for example lpad(cast(x AS string), 5, '0')). Counts rows where e differs from the source's value. Zero means the repair is proven.
- install_guard {"column": c}: installs the source's measured shape of c as a Zetaris data-quality rule on the target and runs it.
- apply_rule {"rule": r}: (queries only) applies one of Nirvana's rewrite rules to the Zetaris query and compares its rows with the source's answer. Rules: cast_drifted_columns (read retyped input columns at their source type), cast_divisions_to_double, explicit_null_order, transpile_from_source.
- cast_output {"columns": [..]}: (queries only) reads the named output columns (all differing ones if the list is empty) at exactly the type Databricks declares for them (see output_types), and compares the rows with the source's answer.
- round_output {"columns": [..], "digits": d}: (queries only) reads the named output columns as DECIMAL(38, d) on Zetaris, the precision the source declares, and compares the rows with the source's answer.
- try_rewrite {}: (queries only) put the full rewritten Spark SQL in a ```sql block AFTER the JSON object, never inside it. Nirvana runs it on Zetaris and compares its rows with the source's answer.
- finish {"summary": s}: stop. Only findings proven by a tool count.

Rules: one tool per reply. Do not repeat a call with the same arguments. Prefer the cheapest tool that settles a question. If memory below already names a repair, verify it first."""


@dataclass
class Step:
    n: int
    tool: str
    args: dict[str, Any]
    why: str
    result: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    ms: float = 0.0
    model_ms: float = 0.0


@dataclass
class Finding:
    column: str | None
    kind: str  # same_values | repair | guard | rewrite
    detail: str
    evidence: str
    expression: str | None = None


@dataclass
class Investigation:
    id: str
    kind: str  # table | query
    subject: str
    candidate: str
    status: str = "running"  # running | done | failed
    context: dict[str, Any] = field(default_factory=dict)
    recalled: list[dict[str, Any]] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    open_columns: list[str] = field(default_factory=list)
    cleared: bool = False
    summary: str = ""
    model: str = ""
    error: str | None = None
    run_id: str = ""
    started_at: float = 0.0
    finished_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ helpers


def is_unavailable(e: Exception) -> bool:
    """A model error that retrying in this investigation cannot fix (quota, auth)."""
    t = str(e).lower()
    return any(k in t for k in ("429", "rate limit", "quota", "refused the token", "401", "403"))


def parse_action(text: str) -> dict[str, Any]:
    """The first JSON object in the model's reply, as {"tool", "args", "why"}. SQL for
    try_rewrite may follow the object in a ```sql block."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    block = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    sql = block.group(1).strip() if block else None
    head = text[: block.start()] if block else text
    m = re.search(r"\{.*\}", head, re.S)
    if not m:
        if sql and re.match(r"(?is)^\s*(select|with)\b", sql):
            return {"tool": "try_rewrite", "args": {"sql": sql}, "why": head.strip()[:300]}
        raise ValueError("no JSON object in the reply: " + text.strip()[:160])
    raw = m.group(0)
    for end in range(len(raw), 0, -1):  # tolerate trailing prose after the object
        if raw[end - 1] != "}":
            continue
        try:
            obj = json.loads(raw[:end])
            break
        except json.JSONDecodeError:
            continue
    else:
        raise ValueError("the reply is not valid JSON")
    if not isinstance(obj, dict) or not isinstance(obj.get("tool"), str):
        raise ValueError("the reply names no tool")
    args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
    if obj["tool"].strip() == "try_rewrite" and not args.get("sql") and sql:
        args = {**args, "sql": sql}
    return {"tool": obj["tool"].strip(), "args": args, "why": str(obj.get("why") or "")[:300]}


_FORBIDDEN = (exp.Select, exp.Subquery, exp.Table, exp.Command, exp.Drop, exp.Insert, exp.Update, exp.Delete, exp.Create)


def safe_expression(expression: str, column: str) -> str:
    """A scalar Spark expression over ``x``, with ``x`` replaced by the target column.
    Refuses anything that reads a table or is more than one expression."""
    if not expression or ";" in expression or len(expression) > 400:
        raise ValueError("the expression must be one short scalar expression")
    try:
        tree = sqlglot.parse_one(expression, read="spark")
    except Exception as e:
        raise ValueError(f"the expression does not parse: {e}") from e
    if any(isinstance(n, _FORBIDDEN) for n in tree.walk()):
        raise ValueError("the expression may not read tables or run statements")
    names = {c.name.lower() for c in tree.find_all(exp.Column)}
    if names - {"x"}:
        raise ValueError(f"the expression may only use x, not {sorted(names - {'x'})}")
    for c in tree.find_all(exp.Column):
        c.replace(exp.column(column, table="c"))
    return tree.sql(dialect="spark")


def differs_sql(column: str, family_s: str, family_t: str, target_expr: str | None = None) -> str:
    """A predicate that is true when the source's and the target's value differ."""
    s, t = f"s.{column}", (target_expr or f"c.{column}")
    if target_expr is None and family_s in NUMERIC and family_t in NUMERIC:
        a, b = f"CAST({s} AS DOUBLE)", f"CAST({t} AS DOUBLE)"
        return f"(({s} IS NULL) <> ({t} IS NULL) OR ABS({a} - {b}) > 1e-9 * GREATEST(1.0, ABS({a})))"
    return f"NOT (CAST({s} AS STRING) <=> CAST({t} AS STRING))"


# ------------------------------------------------------------------ the agent


class Agent:
    def __init__(
        self,
        provider,
        source: Engine,
        target: Engine,
        *,
        source_location: str,
        memory=None,
        clock: Callable[[], float] = time.time,
        on_step: Callable[[Investigation], None] | None = None,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.provider, self.source, self.target = provider, source, target
        self.source_location = source_location
        self.memory, self.clock, self.on_step, self.max_steps = memory, clock, on_step, max_steps
        self._views: set[str] = set()
        self.fallback_model: str | None = None

    # ---------------------------------------------------------------- entry points

    def investigate_table(self, table: str, candidate: str, alignment: dict[str, Any]) -> Investigation:
        inv = Investigation(uuid.uuid4().hex[:10], "table", ident(table), ident(candidate), started_at=self.clock())
        inv.model = getattr(self.provider, "model", "")
        cols = [d["measure"].split(".", 1)[0] for d in alignment.get("differences", [])]
        inv.open_columns = sorted(set(cols))
        inv.context = {
            "table": table,
            "differences": alignment.get("differences", []),
            "type_changes": alignment.get("type_changes", []),
            "error": alignment.get("error"),
        }
        try:
            self._prepare_table(inv)
        except EngineError as e:
            inv.status, inv.error, inv.finished_at = "failed", e.message, self.clock()
            self._emit(inv)
            return inv
        return self._loop(inv)

    def investigate_query(self, query_id: str, candidate: str, judgement: dict[str, Any]) -> Investigation:
        inv = Investigation(uuid.uuid4().hex[:10], "query", query_id, ident(candidate), started_at=self.clock())
        inv.model = getattr(self.provider, "model", "")
        cmp = judgement.get("comparison") or {}
        inv.context = {
            "query_id": query_id,
            "source_sql": judgement.get("source_sql"),
            "target_sql": judgement.get("target_sql"),
            "status": cmp.get("status"),
            "mismatched_columns": cmp.get("mismatched_columns", [])[:12],
            "type_mismatches": len(cmp.get("type_mismatches", [])),
            "source_rows": cmp.get("source_rows"),
            "target_rows": cmp.get("target_rows"),
            "sample": (judgement.get("sample") or {}).get("paired", [])[:2],
            "max_rel_gap": cmp.get("max_rel_gap"),
            "output_types": {t["name"]: t["source"] for t in cmp.get("type_mismatches", []) if t.get("name")},
            "drift": judgement.get("drift") or {},
        }
        inv.open_columns = ["answer"]
        return self._loop(inv)

    # ---------------------------------------------------------------- loop

    def _recall(self, inv: Investigation) -> None:
        if self.memory is None or not hasattr(self.memory, "recall_findings"):
            return
        text = " ".join(f"{t['source']}->{t['target']}" for t in inv.context.get("type_changes", [])) or inv.subject
        text += " " + " ".join(inv.open_columns)
        try:
            inv.recalled = self.memory.recall_findings(text)[:5]
        except Exception as e:  # memory is an accelerator, never a requirement
            inv.recalled = [{"content": f"memory unavailable: {e}", "tags": []}]

    def _prompt(self, inv: Investigation) -> str:
        lines = [f"Case: {inv.kind} {inv.subject}. Target copy on Zetaris: {inv.candidate}. Source: Databricks."]
        facts = {k: v for k, v in inv.context.items() if k not in ("source_sql", "target_sql")}
        lines.append("Facts: " + json.dumps(facts, default=str)[:3000])
        if inv.kind == "query":
            lines.append("The query as it runs on Zetaris (rewrite this text; keep every table name):\n" + str(inv.context.get("target_sql")))
        if inv.recalled:
            lines.append("Memory (Meterless H-MEM) recalls from earlier investigations:")
            lines += [f"- {m.get('content')}" for m in inv.recalled]
        open_ = [c for c in inv.open_columns if not any(f.column == c and f.kind in ("same_values", "repair") for f in inv.findings)]
        lines.append(f"Still unexplained: {open_ or 'nothing'}")
        if inv.steps:
            lines.append("Steps so far:")
            for s in inv.steps:
                lines.append(f"{s.n}. {s.tool} {json.dumps(s.args)} -> {json.dumps(s.result, default=str)[:700]}")
        lines.append("Choose the next tool.")
        return "\n".join(lines)

    def _loop(self, inv: Investigation) -> Investigation:
        self._recall(inv)
        self._emit(inv)
        seen: set[str] = set()
        try:
            for n in range(1, self.max_steps + 1):
                if inv.kind == "table" and inv.open_columns and self._all_explained(inv):
                    inv.summary = inv.summary or "every difference is explained by a proven finding"
                    break
                t0 = time.perf_counter()
                try:
                    reply = self._complete(self._prompt(inv))
                    action = parse_action(reply)
                except Exception as e:
                    step = Step(n, "model", {}, "", {"error": f"no usable action: {e}"}, ok=False, model_ms=(time.perf_counter() - t0) * 1000)
                    inv.steps.append(step)
                    self._emit(inv)
                    if is_unavailable(e) or sum(1 for x in inv.steps[-3:] if x.tool == "model" and not x.ok) >= 3:
                        inv.summary = "stopped: the model route is unavailable (" + str(e)[:120] + ")"
                        break
                    continue
                model_ms = (time.perf_counter() - t0) * 1000
                key = action["tool"] + json.dumps(action["args"], sort_keys=True)
                step = Step(n, action["tool"], action["args"], action["why"], model_ms=model_ms)
                if action["tool"] == "finish":
                    step.result = {"summary": str(action["args"].get("summary", ""))[:500]}
                    inv.steps.append(step)
                    inv.summary = step.result["summary"]
                    break
                if key in seen:
                    step.ok, step.result = False, {"error": "this exact call was already made; choose another"}
                else:
                    seen.add(key)
                    t1 = time.perf_counter()
                    try:
                        step.result = self._run_tool(inv, action["tool"], action["args"])
                    except (EngineError, ValueError, KeyError) as e:
                        step.ok, step.result = False, {"error": e.message if isinstance(e, EngineError) else str(e)}
                    step.ms = (time.perf_counter() - t1) * 1000
                inv.steps.append(step)
                self._emit(inv)
            inv.cleared = inv.kind == "table" and bool(inv.open_columns) and self._all_explained(inv)
            if inv.kind == "query":
                inv.cleared = any(f.kind == "rewrite" for f in inv.findings)
            inv.status = "done"
        except Exception as e:  # report, never crash the server
            inv.status, inv.error = "failed", str(e)[:400]
        inv.finished_at = self.clock()
        self._emit(inv)
        return inv

    def _complete(self, prompt: str) -> str:
        """One model call; a failed call is retried once on the fallback model, if any."""
        try:
            return self.provider.complete(SYSTEM, prompt)
        except Exception:
            fb = getattr(self, "fallback_model", None)
            if not fb or getattr(self.provider, "model", None) in (None, fb):
                raise
            main = self.provider.model
            self.provider.model = fb
            try:
                return self.provider.complete(SYSTEM, prompt)
            finally:
                self.provider.model = main

    def _all_explained(self, inv: Investigation) -> bool:
        done = {f.column for f in inv.findings if f.kind in ("same_values", "repair")}
        return all(c in done for c in inv.open_columns)

    def _emit(self, inv: Investigation) -> None:
        if self.on_step:
            self.on_step(inv)

    # ---------------------------------------------------------------- tools

    def _run_tool(self, inv: Investigation, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if inv.kind == "query" and tool == "try_rewrite":
            return self.try_rewrite(inv, str(args.get("sql", "")))
        if inv.kind == "query" and tool == "apply_rule":
            return self.apply_rule(inv, str(args.get("rule", "")))
        if inv.kind == "query" and tool == "cast_output":
            return self.cast_output(inv, args.get("columns") or [])
        if inv.kind == "query" and tool == "round_output":
            return self.round_output(inv, args.get("columns") or [], args.get("digits", 2))
        if inv.kind == "query" and tool in ("profile_column", "compare_column", "verify_repair", "install_guard"):
            raise ValueError(f"{tool} works on a table case; for a query use try_rewrite")
        col = ident(str(args.get("column", "")))
        if col.lower() not in self._columns(inv):
            raise ValueError(f"{inv.subject} has no column {col}")
        col = self._columns(inv)[col.lower()]
        if tool == "profile_column":
            return self.profile_column(inv, col)
        if tool == "compare_column":
            return self.compare_column(inv, col)
        if tool == "verify_repair":
            return self.verify_repair(inv, col, str(args.get("expression", "")))
        if tool == "install_guard":
            return self.install_guard(inv, col)
        raise ValueError(f"unknown tool {tool}")

    def _prepare_table(self, inv: Investigation) -> None:
        sc = list(self.source.run(f"SELECT * FROM {self.source_location}.{inv.subject} LIMIT 0", max_rows=0).columns)
        tc = list(self.target.run(f"SELECT * FROM {inv.candidate}.{inv.subject} LIMIT 0", max_rows=0).columns)
        inv.context["key"] = sc[0].name
        inv.context["source_types"] = {c.name: c.type for c in sc}
        inv.context["target_types"] = {c.name: c.type for c in tc}

    def _columns(self, inv: Investigation) -> dict[str, str]:
        return {c.lower(): c for c in inv.context.get("source_types", {})}

    def _families(self, inv: Investigation, col: str) -> tuple[str, str]:
        return type_family(inv.context["source_types"][col]), type_family(inv.context["target_types"].get(col, "string"))

    def _view(self, inv: Investigation, col: str) -> str:
        key = inv.context["key"]
        name = f"nv_{inv.subject}_{col}".lower()[:60]
        if name not in self._views:
            cols = key if col == key else f"{key}, {col}"
            self.target.run(f"CREATE DATASOURCE VIEW {name} FROM {DATABRICKS} AS SELECT {cols} FROM {self.source_location}.{inv.subject}", max_rows=0)
            self._views.add(name)
        return f"{DATABRICKS}.{name}"

    def _one(self, engine: Engine, sql: str) -> list[Any]:
        rs = engine.run(sql, max_rows=1)
        return list(rs.rows[0]) if rs.rows else []

    def profile_column(self, inv: Investigation, col: str) -> dict[str, Any]:
        q = (f"SELECT COUNT(*), COUNT({col}), CAST(MIN({col}) AS STRING), CAST(MAX({col}) AS STRING), "
             f"MIN(LENGTH(CAST({col} AS STRING))), MAX(LENGTH(CAST({col} AS STRING))) FROM {{t}}")
        keys = ["rows", "non_null", "min", "max", "min_length", "max_length"]
        s = dict(zip(keys, self._one(self.source, q.format(t=f"{self.source_location}.{inv.subject}"))))
        t = dict(zip(keys, self._one(self.target, q.format(t=f"{inv.candidate}.{inv.subject}"))))
        s["type"], t["type"] = inv.context["source_types"][col], inv.context["target_types"].get(col)
        return {"databricks": s, "zetaris": t}

    def _joined(self, inv: Investigation, col: str, predicate: str, n: int, target_value: str | None = None) -> dict[str, Any]:
        key = inv.context["key"]
        view = self._view(inv, col)
        frm = f"FROM {view} s JOIN {inv.candidate}.{inv.subject} c ON s.{key} = c.{key}"
        total, bad = self._one(self.target, f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END), 0) {frm}")
        tv = target_value or f"c.{col}"
        sample = []
        if int(bad):
            rs = self.target.run(f"SELECT s.{key}, CAST(s.{col} AS STRING), CAST({tv} AS STRING) {frm} WHERE {predicate} LIMIT {int(n)}", max_rows=n)
            sample = [{"key": r[0], "databricks": r[1], "zetaris": r[2]} for r in rs.rows]
        src_rows = int(self._one(self.source, f"SELECT COUNT(*) FROM {self.source_location}.{inv.subject}")[0])
        return {"joined_rows": int(total), "source_rows": src_rows, "differing_rows": int(bad), "examples": sample}

    def compare_column(self, inv: Investigation, col: str) -> dict[str, Any]:
        fs, ft = self._families(inv, col)
        out = self._joined(inv, col, differs_sql(col, fs, ft), 5)
        out["compared_as"] = "numbers" if fs in NUMERIC and ft in NUMERIC else "text"
        if out["differing_rows"] == 0 and out["joined_rows"] == out["source_rows"]:
            how = "as numbers" if out["compared_as"] == "numbers" else "as text"
            self._prove(inv, Finding(col, "same_values", f"every value of {col} is the same on both engines ({how}); only the type changed",
                                     f"{out['joined_rows']} rows joined on {inv.context['key']}, 0 differ"))
            out["proven"] = True
        return out

    def verify_repair(self, inv: Investigation, col: str, expression: str) -> dict[str, Any]:
        repaired = safe_expression(expression, col)
        out = self._joined(inv, col, differs_sql(col, "string", "string", repaired), 5, repaired)
        out["expression"] = expression
        if out["differing_rows"] == 0 and out["joined_rows"] == out["source_rows"]:
            self._prove(inv, Finding(col, "repair", f"{expression} on Zetaris reproduces every Databricks value of {col}",
                                     f"{out['joined_rows']} rows joined on {inv.context['key']}, 0 differ after the repair", expression))
            out["proven"] = True
        return out

    def install_guard(self, inv: Investigation, col: str) -> dict[str, Any]:
        g = build_guard(self.source, f"{self.source_location}.{inv.subject}", self.target, inv.subject, inv.candidate, columns=[col])
        if g.error:
            raise ValueError(g.error)
        rules = [{"rule": r.name, "expression": r.expression, "rows": r.total, "breaking": r.invalid} for r in g.rules]
        for r in g.rules:
            if r.invalid is not None:
                inv.findings.append(Finding(col, "guard", f"Zetaris DQ rule {r.name} installed: {r.invalid} of {r.total} rows break it", r.expression))
        return {"rules": rules}

    def apply_rule(self, inv: Investigation, rule: str) -> dict[str, Any]:
        from . import rewrite as rw

        if rule not in rw.CATALOGUE:
            raise ValueError(f"unknown rule {rule}; choose one of {sorted(rw.CATALOGUE)}")
        ctx = {"drift": inv.context.get("drift") or {}, "source_dialect": "databricks", "target_dialect": "spark", "null_order": "spark"}
        new = rw.chain(inv.context["target_sql"], [rule], ctx)
        if not new:
            raise ValueError(f"{rule} does not change this query")
        out = self.try_rewrite(inv, new, label=f"rule {rule}")
        return out

    def cast_output(self, inv: Investigation, columns: list[Any]) -> dict[str, Any]:
        types: dict[str, str] = {k.lower(): v for k, v in (inv.context.get("output_types") or {}).items()}
        want = {ident(str(c)).lower() for c in columns} or set(types)
        unknown = sorted(want - set(types))
        if unknown:
            raise ValueError(f"no declared source type for {unknown}; output_types lists the columns whose type differs")
        tree = sqlglot.parse_one(inv.context["target_sql"], read="spark")
        select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
        if select is None:
            raise ValueError("no top-level SELECT")
        done = []
        for i, e in enumerate(list(select.expressions)):
            name = e.alias_or_name.lower()
            if name not in want:
                continue
            inner = e.this if isinstance(e, exp.Alias) else e
            dt = exp.DataType.build(types[name], dialect="spark")
            scale = None
            m = re.match(r"(?i)decimal\(\s*\d+\s*,\s*(\d+)\s*\)", types[name])
            if m:
                scale = int(m.group(1))
            value = exp.Round(this=inner.copy(), decimals=exp.Literal.number(scale)) if scale is not None else inner.copy()
            select.expressions[i] = exp.alias_(exp.Cast(this=value, to=dt), e.alias_or_name)
            done.append(name)
        if not done:
            raise ValueError("none of those is an output column of the top-level SELECT")
        out = self.try_rewrite(inv, tree.sql(dialect="spark"), label=f"outputs read at the source's declared types ({len(done)} columns)")
        out["cast"] = done
        return out

    def round_output(self, inv: Investigation, columns: list[Any], digits: Any) -> dict[str, Any]:
        d = int(digits)
        if not 0 <= d <= 10:
            raise ValueError("digits must be between 0 and 10")
        want = {ident(str(c)).lower() for c in columns}
        if not want:
            raise ValueError("name at least one output column")
        tree = sqlglot.parse_one(inv.context["target_sql"], read="spark")
        select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
        if select is None:
            raise ValueError("no top-level SELECT")
        done = []
        for i, e in enumerate(list(select.expressions)):
            name = e.alias_or_name.lower()
            if name in want:
                inner = e.this if isinstance(e, exp.Alias) else e
                select.expressions[i] = exp.alias_(exp.Cast(this=exp.Round(this=inner.copy(), decimals=exp.Literal.number(d)), to=exp.DataType.build(f"DECIMAL(38, {d})")), e.alias_or_name)
                done.append(name)
        if not done:
            raise ValueError(f"none of {sorted(want)} is an output column")
        out = self.try_rewrite(inv, tree.sql(dialect="spark"), label=f"ROUND({', '.join(done)}, {d})")
        out["rounded"] = done
        return out

    def try_rewrite(self, inv: Investigation, sql: str, label: str | None = None) -> dict[str, Any]:
        sql = sql.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", sql):
            raise ValueError("no SQL arrived: put the full statement in a ```sql block after the JSON, or use apply_rule / round_output, which build the SQL for you")
        if not hasattr(self, "_source_answer"):
            self._source_answer = self.source.run(inv.context["source_sql"], max_rows=100_000)
        src = self._source_answer
        tgt = self.target.run(sql, max_rows=100_000)
        c = compare(src, tgt, ordered=bool(re.search(r"(?is)\border\s+by\b", inv.context["source_sql"] or "")), rel_tol=0.0)
        out = {"status": c.status, "source_rows": c.source_rows, "target_rows": c.target_rows, "mismatched_columns": c.mismatched_columns()[:10],
               "source_fp": c.source_fp[:12], "target_fp": c.target_fp[:12]}
        if c.status == "identical":
            self._prove(inv, Finding("answer", "rewrite", f"the rewrite ({label or 'model-written SQL'}) returns Databricks' exact rows on Zetaris", f"fingerprints equal ({c.source_fp[:12]})", sql))
            out["proven"] = True
        return out

    def _prove(self, inv: Investigation, f: Finding) -> None:
        if any(x.column == f.column and x.kind == f.kind for x in inv.findings):
            return
        inv.findings.append(f)
        if self.memory is not None and hasattr(self.memory, "remember_finding"):
            changes = {t["column"]: f"{t['source']}->{t['target']}" for t in inv.context.get("type_changes", [])}
            change = changes.get(f.column or "", "")
            text = f"{inv.kind} {inv.subject} column {f.column}: {f.detail}" + (f" (type {change})" if change else "")
            tags = [f"finding:{f.kind}", f"table:{inv.subject}", f"column:{f.column}"] + ([f"change:{change}"] if change else [])
            if f.expression:
                tags.append("repair:" + f.expression[:200])
            try:
                self.memory.remember_finding(text, tags)
            except Exception:
                pass


# ------------------------------------------------------------------ store and background runs


class InvestigationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.live: dict[str, Investigation] = {}
        self.lock = threading.Lock()

    def put(self, inv: Investigation) -> None:
        with self.lock:
            self.live[inv.id] = inv
            if inv.status != "running":
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(json.dumps(inv.as_dict(), default=str) + "\n")

    def all(self) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    x = json.loads(line)
                    out[x["id"]] = x
                except (json.JSONDecodeError, KeyError):
                    continue
        with self.lock:
            for k, v in self.live.items():
                out[k] = v.as_dict()
        return sorted(out.values(), key=lambda x: -(x.get("started_at") or 0))

    def get(self, inv_id: str) -> dict[str, Any] | None:
        return next((x for x in self.all() if x["id"] == inv_id), None)
