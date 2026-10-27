"""Candidate rewrites. Each takes the target's SQL text and returns a new text (or None when
it does not apply). A rewrite is only ever kept after the target runs it and the rows equal
the source's original answer - nothing here is trusted on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import sqlglot
from sqlglot import exp

DIALECT = "spark"


@dataclass(frozen=True)
class Rewrite:
    name: str
    describe: str
    apply: Callable[[str, dict], str | None]


def _tree(sql: str) -> exp.Expression:
    t = sqlglot.parse_one(sql, read=DIALECT)
    if t is None:
        raise ValueError("empty statement")
    return t


def drop_top_limit(sql: str) -> str | None:
    t = _tree(sql)
    if t.args.get("limit") is None:
        return None
    t.set("limit", None)
    return t.sql(dialect=DIALECT)


def explicit_null_order(sql: str, ctx: dict) -> str | None:
    """Write the source's NULL placement into every ORDER BY key of the outer query.
    ``ctx['null_order']`` is the source's default: "spark" puts NULLs first ascending and last
    descending (Spark, Databricks); "last" puts them last both ways (DuckDB). An engine with
    a different default orders the same rows differently."""
    t = _tree(sql)
    order = t.args.get("order")
    if order is None:
        return None
    convention = ctx.get("null_order", "spark")
    changed = False
    for i, o in enumerate(list(order.expressions)):
        if not isinstance(o, exp.Ordered):
            continue
        desc = bool(o.args.get("desc"))
        want_first = (not desc) if convention == "spark" else False
        text = o.sql(dialect=DIALECT)
        if "NULLS" in text.upper():
            continue
        # Spark's generator drops a NULLS clause that matches its own default, so the clause
        # is written as raw text to make it explicit for an engine with another default.
        raw = f"{o.this.sql(dialect=DIALECT)} {'DESC' if desc else 'ASC'} NULLS {'FIRST' if want_first else 'LAST'}"
        order.expressions[i] = exp.var(raw)
        changed = True
    return t.sql(dialect=DIALECT) if changed else None


def total_order(sql: str, ctx: dict) -> str | None:
    """Break ties at a LIMIT by adding every output column to the ORDER BY. Applied to BOTH
    engines, it makes the cut deterministic; used as a probe, not as a migration rewrite."""
    t = _tree(sql)
    order = t.args.get("order")
    if order is None or not isinstance(t, exp.Select):
        return None
    have = {o.this.sql(dialect=DIALECT) for o in order.expressions if isinstance(o, exp.Ordered)}
    added = False
    for i, proj in enumerate(t.expressions, start=1):
        key = exp.Literal.number(i)
        if proj.alias_or_name and proj.alias_or_name not in have:
            order.append("expressions", exp.Ordered(this=key, nulls_first=True))
            added = True
    return t.sql(dialect=DIALECT) if added else None


def cast_divisions_to_double(sql: str, ctx: dict) -> str | None:
    """Make every division floating point: ``a / b`` -> ``CAST(a AS DOUBLE) / b``.
    Fixes engines that divide two integers as integers (7 / 2 = 3)."""
    t = _tree(sql)
    changed = False
    # Innermost first, so a / b / c becomes CAST(CAST(a AS DOUBLE) / b AS DOUBLE) / c.
    for d in reversed(list(t.find_all(exp.Div))):
        left = d.this
        if isinstance(left, exp.Cast) and left.to.is_type("double"):
            continue
        d.set("this", exp.Cast(this=left, to=exp.DataType.build("double")))
        changed = True
    return t.sql(dialect=DIALECT) if changed else None


def cast_drifted_columns(sql: str, ctx: dict) -> str | None:
    """Read columns whose type changed on the target back at the source's type:
    ``ss_net_paid`` -> ``CAST(ss_net_paid AS DECIMAL(7,2))``. ``ctx['drift']`` maps a
    lower-case column name to the source type."""
    drift: dict[str, str] = ctx.get("drift") or {}
    if not drift:
        return None
    t = _tree(sql)
    changed = False
    for col in list(t.find_all(exp.Column)):
        name = col.name.lower()
        if name not in drift:
            continue
        parent = col.parent
        if isinstance(parent, exp.Cast):
            continue
        # Never rewrite a column used as an alias target or in GROUP BY / ORDER BY by position.
        col.replace(exp.Cast(this=col.copy(), to=exp.DataType.build(drift[name], dialect=DIALECT)))
        changed = True
    return t.sql(dialect=DIALECT) if changed else None


def transpile_from_source(sql: str, ctx: dict) -> str | None:
    """Let sqlglot render the source-dialect text for the target dialect."""
    src, tgt = ctx.get("source_dialect"), ctx.get("target_dialect")
    if not src or not tgt or src == tgt:
        return None
    out = sqlglot.transpile(sql, read=src, write=tgt)[0]
    return None if out == sql else out


CATALOGUE: dict[str, Rewrite] = {
    r.name: r
    for r in (
        Rewrite("cast_drifted_columns", "read retyped columns back at the source type", cast_drifted_columns),
        Rewrite("cast_divisions_to_double", "make every division floating point", cast_divisions_to_double),
        Rewrite("explicit_null_order", "write the source's NULL placement into ORDER BY", explicit_null_order),
        Rewrite("transpile_from_source", "render the source dialect for the target", transpile_from_source),
    )
}


def chain(sql: str, names: list[str], ctx: dict) -> str | None:
    """Apply rewrites in order; None if none of them changed anything."""
    out, changed = sql, False
    for n in names:
        nxt = CATALOGUE[n].apply(out, ctx)
        if nxt is not None:
            out, changed = nxt, True
    return out if changed else None
