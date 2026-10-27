"""Guard rules: the source's measured shape, enforced by Zetaris as data-quality rules.

Alignment compares two copies once. After a migration the target keeps changing, so Nirvana
turns what it measured on the source into rules that Zetaris itself keeps checking: for every
column, the range of values (or of text lengths) Databricks holds and whether it is ever NULL.
The rules live on a Zetaris Unified Semantic Layer table over the target copy
(``COMPILE USL`` / ``ACTIVATE USL TABLE``), are registered with ``REGISTER DQ`` and are run with
``RUN DQ``; ``SHOW DQ INVALID RECORD`` returns the rows that break each one.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .engines.base import Engine
from .model import Column, EngineError

NAMESPACE = "lightning.metastore.nirvana"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ident(name: str) -> str:
    if not _IDENT.match(name or ""):
        raise ValueError(f"not a plain identifier: {name!r}")
    return name


@dataclass
class Rule:
    name: str
    column: str
    expression: str
    why: str
    total: int | None = None
    valid: int | None = None
    invalid: int | None = None
    sample: list[list[Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class Guard:
    table: str
    source: str
    candidate: str
    usl: str
    rules: list[Rule] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    sql: list[str] = field(default_factory=list)
    error: str | None = None
    ms: float = 0.0
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["broken"] = sum(1 for r in self.rules if r.invalid)
        return d


def usl_type(t: str) -> str:
    """A USL DDL type for a Zetaris (Spark) column type."""
    t = t.strip().strip('"').lower()
    if t in ("integer", "int"):
        return "int"
    if t in ("long", "bigint"):
        return "bigint"
    if t in ("short", "smallint", "byte", "tinyint"):
        return "int"
    if t in ("double", "float", "real"):
        return "double"
    if t.startswith("decimal"):
        return t
    if t in ("date", "timestamp", "boolean"):
        return t
    return "varchar(1000)"


def profile_sql(ref: str, columns: list[Column]) -> str:
    """One statement for the source: row count, and per column the non-null count and the
    range of values (numbers, dates) or of text lengths."""
    parts = ["COUNT(*) AS n_rows"]
    for i, c in enumerate(columns):
        q = f"`{ident(c.name)}`"
        parts.append(f"COUNT({q}) AS c{i}_n")
        if c.family == "string":
            parts += [f"MIN(LENGTH({q})) AS c{i}_lo", f"MAX(LENGTH({q})) AS c{i}_hi"]
        else:
            parts += [f"CAST(MIN({q}) AS STRING) AS c{i}_lo", f"CAST(MAX({q}) AS STRING) AS c{i}_hi"]
    return f"SELECT {', '.join(parts)} FROM {ref}"


def _literal(value: str, family: str) -> str | None:
    v = str(value).strip()
    if family == "date" and _DATE.match(v):
        return f"DATE '{v}'"
    if family in ("integer", "float", "decimal") and _NUMBER.match(v):
        return v
    return None


def derive_rules(columns: list[Column], values: list[Any]) -> list[Rule]:
    """Rules from one profile row (``profile_sql``'s columns, in order)."""
    n_rows = int(values[0])
    rules: list[Rule] = []
    for i, c in enumerate(columns):
        nn, lo, hi = values[1 + 3 * i: 4 + 3 * i]
        col = ident(c.name)
        never_null = nn is not None and int(nn) == n_rows and n_rows > 0
        checks, why = [], []
        if c.family == "string":
            if lo is not None and hi is not None:
                a, b = int(lo), int(hi)
                checks.append(f"length(cast({col} AS string)) = {a}" if a == b else f"length(cast({col} AS string)) BETWEEN {a} AND {b}")
                why.append(f"Databricks text is {a} characters long" if a == b else f"Databricks text is {a} to {b} characters long")
        else:
            la, lb = (_literal(lo, c.family), _literal(hi, c.family)) if lo is not None and hi is not None else (None, None)
            if la and lb:
                checks.append(f"{col} BETWEEN {la} AND {lb}")
                why.append(f"Databricks holds {lo} to {hi}")
        if not checks and not never_null:
            continue
        body = " AND ".join(checks)
        if never_null:
            expr = f"{col} IS NOT NULL" + (f" AND {body}" if body else "")
            why.insert(0, "never NULL on Databricks")
        else:
            expr = f"{col} IS NULL OR ({body})"
        rules.append(Rule(f"{col}_shape", col, expr, "; ".join(why)))
    return rules


def usl_name(table: str, candidate: str) -> str:
    return f"guard_{ident(candidate).lower()}_{ident(table).lower()}"


def compile_sql(usl: str, table: str, target_columns: list[Column]) -> str:
    cols = ", ".join(f"{ident(c.name)} {usl_type(c.type)}" for c in target_columns)
    return f"COMPILE USL IF NOT EXISTS {usl} DEPLOY NAMESPACE {NAMESPACE} DDL\nCREATE TABLE {ident(table)} ({cols})"


def build_guard(source: Engine, source_ref: str, target: Engine, table: str, candidate: str, *, sample: int = 5, columns: list[str] | None = None, clock=time.time) -> Guard:
    """Measure the source, install the rules on Zetaris over ``candidate.table``, run them."""
    started = time.perf_counter()
    try:
        usl = usl_name(table, candidate)
    except ValueError as e:
        return Guard(table, source_ref, candidate, "", error=str(e), at=clock())
    g = Guard(table, source_ref, candidate, usl, at=clock())
    fq = f"{NAMESPACE}.{usl}.{ident(table)}"
    try:
        scols = list(source.run(f"SELECT * FROM {source_ref} LIMIT 0", max_rows=0).columns)
        tcols = list(target.run(f"SELECT * FROM {ident(candidate)}.{ident(table)} LIMIT 0", max_rows=0).columns)
        if columns:
            keep = {c.lower() for c in columns}
            scols = [c for c in scols if c.name.lower() in keep]
        shared = {c.name.lower() for c in tcols}
        scols = [c for c in scols if c.name.lower() in shared]
        g.columns = [c.name for c in tcols]
        prof = profile_sql(source_ref, scols)
        g.sql.append(prof)
        rs = source.run(prof, max_rows=1)
        g.rules = derive_rules(scols, list(rs.rows[0]))
        steps = [
            f"CREATE NAMESPACE IF NOT EXISTS {NAMESPACE.rsplit('.', 1)[0]}",
            f"CREATE NAMESPACE IF NOT EXISTS {NAMESPACE}",
            compile_sql(usl, table, tcols),
            f"ACTIVATE USL TABLE {fq} AS SELECT {', '.join(ident(c.name) for c in tcols)} FROM {ident(candidate)}.{ident(table)}",
        ]
        for s in steps:
            g.sql.append(s)
            target.run(s, max_rows=0)
    except (EngineError, ValueError, IndexError) as e:
        g.error = e.message if isinstance(e, EngineError) else str(e)
        g.ms = (time.perf_counter() - started) * 1000
        return g
    for r in g.rules:
        reg = f"REGISTER DQ {r.name} TABLE {fq} AS {r.expression}"
        try:
            g.sql.append(reg)
            try:
                target.run(reg, max_rows=0)
            except EngineError as e:
                if "exist" not in e.message.lower():
                    raise
                upd = f"UPDATE DQ {r.name} TABLE {fq} SET NAME = {r.name}, EXPRESSION = {r.expression}"
                g.sql.append(upd)
                target.run(upd, max_rows=0)
            out = target.run(f"RUN DQ {r.name} TABLE {fq}", max_rows=5)
            names = [c.name for c in out.columns]
            row = dict(zip(names, out.rows[0])) if out.rows else {}
            r.total = int(row["total_record"]) if row.get("total_record") is not None else None
            r.valid = int(row["valid_record"]) if row.get("valid_record") is not None else None
            r.invalid = int(row["invalid_record"]) if row.get("invalid_record") is not None else None
            if r.invalid:
                bad = target.run(f"SHOW DQ INVALID RECORD {r.name} TABLE {fq}", max_rows=sample)
                r.sample = [list(x) for x in bad.rows[:sample]]
        except (EngineError, ValueError, KeyError) as e:
            r.error = e.message if isinstance(e, EngineError) else str(e)
    g.ms = (time.perf_counter() - started) * 1000
    return g


class GuardStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, g: Guard) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(g.as_dict(), default=str) + "\n")

    def latest(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        seen: dict[tuple, dict[str, Any]] = {}
        for line in self.path.read_text().splitlines():
            try:
                x = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen[(x["table"], x["candidate"])] = x
        return sorted(seen.values(), key=lambda x: -x["at"])
