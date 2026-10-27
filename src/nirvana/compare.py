"""Compare two result sets the way a migration sign-off needs: every row, every value.

The comparison is a multiset diff of canonical rows (duplicates count), plus an ordered
check when the query's outermost ``ORDER BY`` makes order part of the answer. Rows that
appear on one side only are paired up (same count on both sides, sorted by their text
form) so a changed value shows up as "column 3 differs" rather than "one row missing,
one row extra".

Nothing here is approximate unless the caller asks: ``tolerance`` is only used to label a
difference ``float_noise`` (every mismatch is numeric and within the relative bound). The
verdict still records that the answers are not identical.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .canon import Token, fingerprint, row_key, row_tokens
from .model import ResultSet

DEFAULT_REL_TOL = 1e-9
SAMPLE = 20


@dataclass(frozen=True)
class CellDiff:
    column: int
    name: str
    source: Token
    target: Token
    numeric_gap: float | None  # |a - b| when both sides are numbers


@dataclass(frozen=True)
class RowPair:
    source: tuple[Token, ...]
    target: tuple[Token, ...]
    cells: tuple[CellDiff, ...]


@dataclass
class Comparison:
    status: str  # identical | order_only | float_noise | different | schema_mismatch
    source_rows: int
    target_rows: int
    source_fp: str
    target_fp: str
    ordered: bool
    ordered_equal: bool
    column_count_equal: bool
    type_mismatches: list[dict[str, Any]] = field(default_factory=list)
    only_in_source: int = 0
    only_in_target: int = 0
    paired: list[RowPair] = field(default_factory=list)
    unpaired_source: list[tuple[Token, ...]] = field(default_factory=list)
    unpaired_target: list[tuple[Token, ...]] = field(default_factory=list)
    max_rel_gap: float = 0.0

    @property
    def identical(self) -> bool:
        return self.status == "identical"

    def mismatched_columns(self) -> list[str]:
        seen: dict[str, None] = {}
        for p in self.paired:
            for c in p.cells:
                seen.setdefault(c.name, None)
        return list(seen)

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_rows": self.source_rows,
            "target_rows": self.target_rows,
            "source_fp": self.source_fp,
            "target_fp": self.target_fp,
            "ordered": self.ordered,
            "ordered_equal": self.ordered_equal,
            "column_count_equal": self.column_count_equal,
            "type_mismatches": self.type_mismatches,
            "only_in_source": self.only_in_source,
            "only_in_target": self.only_in_target,
            "mismatched_columns": self.mismatched_columns(),
            "max_rel_gap": self.max_rel_gap,
        }


def _as_number(t: Token) -> float | Decimal | None:
    kind, value = t
    if kind == "num":
        return Decimal(value)
    if kind == "float":
        return value
    return None


def numeric_gap(a: Token, b: Token) -> tuple[float, float] | None:
    """(absolute gap, relative gap) when both tokens are numbers, else None."""
    x, y = _as_number(a), _as_number(b)
    if x is None or y is None:
        return None
    fx, fy = float(x), float(y)
    if math.isnan(fx) or math.isnan(fy):
        return (0.0, 0.0) if math.isnan(fx) and math.isnan(fy) else (math.inf, math.inf)
    if math.isinf(fx) or math.isinf(fy):
        return (0.0, 0.0) if fx == fy else (math.inf, math.inf)
    if isinstance(x, Decimal) and isinstance(y, Decimal):
        gap = float(abs(x - y))
    else:
        gap = abs(fx - fy)
    scale = max(abs(fx), abs(fy))
    rel = 0.0 if gap == 0 else (gap / scale if scale else math.inf)
    return gap, rel


def _cells(src: tuple[Token, ...], tgt: tuple[Token, ...], names: list[str]) -> tuple[CellDiff, ...]:
    out = []
    for i, (a, b) in enumerate(zip(src, tgt)):
        if a != b:
            g = numeric_gap(a, b)
            out.append(CellDiff(i, names[i], a, b, None if g is None else g[0]))
    return tuple(out)


PAIR_LIMIT = 400


def _pair(
    left: list[tuple[Token, ...]], right: list[tuple[Token, ...]]
) -> list[tuple[tuple[Token, ...], tuple[Token, ...]]]:
    """Pair each left row with the right row that shares the most equal cells (greedy, in
    order). Above PAIR_LIMIT rows the sorted order is used instead, which is exact when only
    one column changed and the changed column sorts after the identifying ones."""
    if len(left) > PAIR_LIMIT:
        return list(zip(left, right))
    free = list(range(len(right)))
    out = []
    for a in left:
        best_i, best_score = free[0], -1
        for i in free:
            score = sum(1 for x, y in zip(a, right[i]) if x == y)
            if score > best_score:
                best_i, best_score = i, score
        free.remove(best_i)
        out.append((a, right[best_i]))
    return out


def compare(
    source: ResultSet,
    target: ResultSet,
    *,
    ordered: bool = False,
    rel_tol: float = DEFAULT_REL_TOL,
) -> Comparison:
    names = source.column_names
    width_equal = len(source.columns) == len(target.columns)
    type_mismatches = []
    for i, (a, b) in enumerate(zip(source.columns, target.columns)):
        if a.family != b.family:
            type_mismatches.append({"column": i, "name": a.name, "source": a.type, "target": b.type})

    src_rows = [row_tokens(r, source.columns) for r in source.rows]
    tgt_rows = [row_tokens(r, target.columns) for r in target.rows]
    s_fp = fingerprint(src_rows, ordered=False)
    t_fp = fingerprint(tgt_rows, ordered=False)
    ordered_equal = [row_key(r) for r in src_rows] == [row_key(r) for r in tgt_rows]

    base: dict[str, Any] = dict(
        source_rows=len(src_rows),
        target_rows=len(tgt_rows),
        source_fp=s_fp,
        target_fp=t_fp,
        ordered=ordered,
        ordered_equal=ordered_equal,
        column_count_equal=width_equal,
        type_mismatches=type_mismatches,
    )
    if not width_equal:
        return Comparison(status="schema_mismatch", **base)

    if s_fp == t_fp:
        status = "identical" if (ordered_equal or not ordered) else "order_only"
        return Comparison(status=status, **base)

    s_count = Counter(row_key(r) for r in src_rows)
    t_count = Counter(row_key(r) for r in tgt_rows)
    by_key_s = {row_key(r): r for r in src_rows}
    by_key_t = {row_key(r): r for r in tgt_rows}
    only_s: list[tuple[Token, ...]] = []
    only_t: list[tuple[Token, ...]] = []
    for k in sorted(s_count):
        extra = s_count[k] - t_count.get(k, 0)
        only_s.extend([by_key_s[k]] * max(extra, 0))
    for k in sorted(t_count):
        extra = t_count[k] - s_count.get(k, 0)
        only_t.extend([by_key_t[k]] * max(extra, 0))

    paired: list[RowPair] = []
    max_rel = 0.0
    all_numeric_close = True
    if len(only_s) == len(only_t):
        for a, b in _pair(only_s, only_t):
            cells = _cells(a, b, names)
            paired.append(RowPair(a, b, cells))
            for c in cells:
                g = numeric_gap(c.source, c.target)
                if g is None:
                    all_numeric_close = False
                else:
                    max_rel = max(max_rel, g[1])
                    if g[1] > rel_tol:
                        all_numeric_close = False
        unpaired_s: list[tuple[Token, ...]] = []
        unpaired_t: list[tuple[Token, ...]] = []
    else:
        all_numeric_close = False
        unpaired_s, unpaired_t = only_s, only_t

    status = "float_noise" if (paired and all_numeric_close) else "different"
    return Comparison(
        status=status,
        only_in_source=len(only_s),
        only_in_target=len(only_t),
        paired=paired[:SAMPLE],
        unpaired_source=unpaired_s[:SAMPLE],
        unpaired_target=unpaired_t[:SAMPLE],
        max_rel_gap=max_rel,
        **base,
    )
