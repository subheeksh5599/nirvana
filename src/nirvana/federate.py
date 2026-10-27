"""Row-level differences between two copies of a table, computed by Zetaris in one federated query.

Alignment says *that* two copies of a table differ (row counts, sums). When both copies are
reachable from Zetaris Lightning, possibly from different stores (a public S3 bucket and a
local filesystem, say), Zetaris can say *which rows* differ: ``EXCEPT ALL`` across the two
sources, in each direction, keeps duplicates, so a row that appears three times on one side
and once on the other is reported twice.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .engines.base import Engine
from .model import EngineError


@dataclass
class RowDiff:
    table: str
    reference: str
    candidate: str
    only_in_reference: int | None = None
    only_in_candidate: int | None = None
    sample_reference: list[list[Any]] = field(default_factory=list)
    sample_candidate: list[list[Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    sql: list[str] = field(default_factory=list)
    ms: float = 0.0
    error: str | None = None
    at: float = 0.0

    @property
    def identical(self) -> bool:
        return self.error is None and self.only_in_reference == 0 and self.only_in_candidate == 0

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "identical": self.identical}


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"not a plain identifier: {name!r}")
    return name


def except_all(left: str, right: str) -> str:
    return f"SELECT * FROM {left} EXCEPT ALL SELECT * FROM {right}"


def diff_sql(table: str, reference: str, candidate: str, sample: int) -> dict[str, str]:
    t, r, c = _ident(table), _ident(reference), _ident(candidate)
    ref, cand = f"{r}.{t}", f"{c}.{t}"
    return {
        "count_reference": f"SELECT COUNT(*) AS n FROM ({except_all(ref, cand)}) d",
        "count_candidate": f"SELECT COUNT(*) AS n FROM ({except_all(cand, ref)}) d",
        "sample_reference": f"SELECT * FROM ({except_all(ref, cand)}) d LIMIT {int(sample)}",
        "sample_candidate": f"SELECT * FROM ({except_all(cand, ref)}) d LIMIT {int(sample)}",
    }


def row_diff(engine: Engine, table: str, reference: str, candidate: str, *, sample: int = 5, clock=time.time) -> RowDiff:
    """Count and sample the rows only in each copy. Four statements, all run on ``engine``."""
    d = RowDiff(table, reference, candidate, at=clock())
    try:
        qs = diff_sql(table, reference, candidate, sample)
    except ValueError as e:
        d.error = str(e)
        return d
    d.sql = list(qs.values())
    started = time.perf_counter()
    try:
        d.only_in_reference = int(engine.run(qs["count_reference"], max_rows=1).rows[0][0])
        d.only_in_candidate = int(engine.run(qs["count_candidate"], max_rows=1).rows[0][0])
        if d.only_in_reference:
            rs = engine.run(qs["sample_reference"], max_rows=sample)
            d.columns, d.sample_reference = [c.name for c in rs.columns], [list(r) for r in rs.rows]
        if d.only_in_candidate:
            rs = engine.run(qs["sample_candidate"], max_rows=sample)
            d.columns = d.columns or [c.name for c in rs.columns]
            d.sample_candidate = [list(r) for r in rs.rows]
    except EngineError as e:
        d.error = e.message
    except (IndexError, TypeError, ValueError) as e:
        d.error = f"unexpected answer: {e}"
    d.ms = (time.perf_counter() - started) * 1000
    return d


class DiffStore:
    """The latest row diff per (table, reference, candidate), kept as JSON lines."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, d: RowDiff) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(d.as_dict(), default=str) + "\n")

    def latest(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        seen: dict[tuple, dict[str, Any]] = {}
        for line in self.path.read_text().splitlines():
            try:
                x = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen[(x["table"], x["reference"], x["candidate"])] = x
        return sorted(seen.values(), key=lambda x: -x["at"])
