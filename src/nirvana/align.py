"""Source alignment: prove both engines hold the same input data before judging any query.

A query diff is only meaningful when the tables under it match. For every workload table
both engines run one profile statement: row count, and per column the non-null count, MIN,
MAX, and an exact SUM (numbers cast to DECIMAL(38, s) where s is the scale the source declares, so a DECIMAL(7,2)
column on one side and a DOUBLE or REAL column holding the same cents on the other still
compare exactly) or the summed
length (text). A table is

* ``aligned``        - every profile value equal and every column the same type family
* ``aligned_types``  - every profile value equal, but some column changed type family
                        (the data is the same; arithmetic on it may not be)
* ``not_aligned``    - row count or a profile value differs; queries reading it are blocked
* ``missing``        - one engine could not read the table
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .canon import token
from .compare import numeric_gap
from .engines import Engine
from .model import Column, EngineError, decimal_scale
from .workload import split_location

NUMERIC = ("integer", "decimal", "float")


def table_ref(location: str, table: str) -> str:
    catalog, schema = split_location(location)
    return f"{catalog}.{schema}.{table}" if catalog else f"{schema}.{table}"


def measure_scale(c: Column) -> int:
    """Digits after the point at which a numeric column is measured: the source's declared
    scale for DECIMAL, 0 for integers, 6 for floating point."""
    if c.family == "integer":
        return 0
    if c.family == "decimal":
        s = decimal_scale(c.type)
        return 6 if s is None else s
    return 6


def profile_sql(ref: str, columns: list[Column]) -> tuple[str, list[str]]:
    """One SELECT that profiles every column; returns (sql, the measure names in order)."""
    parts = ["COUNT(*) AS n_rows"]
    measures = ["rows"]
    for i, c in enumerate(columns):
        q = f"`{c.name}`"
        parts.append(f"COUNT({q}) AS c{i}_nonnull")
        measures.append(f"{c.name}.nonnull")
        fam = c.family
        if fam in NUMERIC:
            dec = f"DECIMAL(38,{measure_scale(c)})"
            parts.append(f"SUM(CAST({q} AS {dec})) AS c{i}_sum")
            measures.append(f"{c.name}.sum")
            parts.append(f"MIN(CAST({q} AS {dec})) AS c{i}_min")
            measures.append(f"{c.name}.min")
            parts.append(f"MAX(CAST({q} AS {dec})) AS c{i}_max")
            measures.append(f"{c.name}.max")
        elif fam == "string":
            parts.append(f"SUM(LENGTH({q})) AS c{i}_len")
            measures.append(f"{c.name}.length")
            parts.append(f"MIN({q}) AS c{i}_min")
            measures.append(f"{c.name}.min")
            parts.append(f"MAX({q}) AS c{i}_max")
            measures.append(f"{c.name}.max")
        elif fam in ("date", "timestamp"):
            parts.append(f"MIN({q}) AS c{i}_min")
            measures.append(f"{c.name}.min")
            parts.append(f"MAX({q}) AS c{i}_max")
            measures.append(f"{c.name}.max")
    return f"SELECT {', '.join(parts)} FROM {ref}", measures


@dataclass
class TableAlignment:
    table: str
    status: str
    source_rows: int | None = None
    target_rows: int | None = None
    type_changes: list[dict[str, str]] = field(default_factory=list)
    differences: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    source_sql: str | None = None
    target_sql: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in ("aligned", "aligned_types", "explained")

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "status": self.status,
            "source_rows": self.source_rows,
            "target_rows": self.target_rows,
            "type_changes": self.type_changes,
            "differences": self.differences,
            "error": self.error,
            "source_sql": self.source_sql,
            "target_sql": self.target_sql,
        }


def _columns(engine: Engine, ref: str) -> list[Column]:
    return list(engine.run(f"SELECT * FROM {ref} LIMIT 0", max_rows=0).columns)


def _profile_values(engine: Engine, sql: str, n: int) -> list[Any]:
    rs = engine.run(sql, max_rows=1)
    if rs.row_count != 1 or len(rs.rows[0]) != n:
        raise EngineError(engine.name, f"profile returned {rs.row_count} rows of {len(rs.columns)} columns, expected 1 of {n}")
    return list(rs.rows[0])


def _same(a: Any, b: Any, family: str) -> bool:
    ta, tb = token(a, family), token(b, family)
    if ta == tb:
        return True
    g = numeric_gap(ta, tb)
    return g is not None and g[0] == 0


def align_table(source: Engine, target: Engine, table: str) -> TableAlignment:
    sref, tref = table_ref(source.location, table), table_ref(target.location, table)
    try:
        scols = _columns(source, sref)
    except EngineError as e:
        return TableAlignment(table, "missing", error=f"{source.name}: {e.message}")
    try:
        tcols = _columns(target, tref)
    except EngineError as e:
        return TableAlignment(table, "missing", error=f"{target.name}: {e.message}")

    tmap = {c.name.lower(): c for c in tcols}
    missing = [c.name for c in scols if c.name.lower() not in tmap]
    if missing:
        return TableAlignment(table, "not_aligned", error=f"columns missing on {target.name}: {', '.join(missing)}")
    paired_target = [Column(tmap[c.name.lower()].name, tmap[c.name.lower()].type) for c in scols]
    type_changes = [
        {"column": s.name, "source": s.type, "target": t.type}
        for s, t in zip(scols, paired_target)
        if s.family != t.family
    ]
    # Profile both sides with the SOURCE column families so the measures line up.
    profile_cols = [Column(t.name, s.type) for s, t in zip(scols, paired_target)]
    s_sql, measures = profile_sql(sref, scols)
    t_sql, _ = profile_sql(tref, profile_cols)
    try:
        svals = _profile_values(source, s_sql, len(measures))
        tvals = _profile_values(target, t_sql, len(measures))
    except EngineError as e:
        return TableAlignment(table, "missing", error=e.message, type_changes=type_changes, source_sql=s_sql, target_sql=t_sql)

    families = ["integer"]
    for c in scols:
        fam = c.family
        if fam in NUMERIC:
            families += ["integer", "decimal", "decimal", "decimal"]
        elif fam == "string":
            families += ["integer", "integer", "string", "string"]
        elif fam in ("date", "timestamp"):
            families += ["integer", fam, fam]
        else:
            families += ["integer"]
    diffs = [
        {"measure": m, "source": _plain(a), "target": _plain(b)}
        for m, a, b, f in zip(measures, svals, tvals, families)
        if not _same(a, b, f)
    ]
    status = "not_aligned" if diffs else ("aligned_types" if type_changes else "aligned")
    return TableAlignment(
        table,
        status,
        source_rows=int(token(svals[0], "integer")[1]) if svals[0] is not None else None,
        target_rows=int(token(tvals[0], "integer")[1]) if tvals[0] is not None else None,
        type_changes=type_changes,
        differences=diffs,
        source_sql=s_sql,
        target_sql=t_sql,
    )


def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)
