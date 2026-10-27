"""Core value types shared by engines, the comparator and the ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


class EngineError(RuntimeError):
    """An engine refused or failed a statement. ``message`` is the engine's own text."""

    def __init__(self, engine: str, message: str, *, sql: str | None = None) -> None:
        super().__init__(f"{engine}: {message}")
        self.engine = engine
        self.message = message
        self.sql = sql


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # the engine's own type name, lower-cased, e.g. "decimal(7,2)", "double", "string"

    @property
    def family(self) -> str:
        return type_family(self.type)


@dataclass(frozen=True)
class ResultSet:
    """Rows exactly as one engine returned them, plus how they were obtained."""

    engine: str
    sql: str
    columns: tuple[Column, ...]
    rows: tuple[tuple[Any, ...], ...]
    elapsed_ms: float = 0.0
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @staticmethod
    def build(
        engine: str,
        sql: str,
        columns: Sequence[Column],
        rows: Sequence[Sequence[Any]],
        elapsed_ms: float = 0.0,
        truncated: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> "ResultSet":
        width = len(columns)
        fixed = []
        for i, row in enumerate(rows):
            if len(row) != width:
                raise ValueError(f"row {i} has {len(row)} values, expected {width}")
            fixed.append(tuple(row))
        return ResultSet(engine, sql, tuple(columns), tuple(fixed), elapsed_ms, truncated, meta or {})


_INTEGER = ("tinyint", "smallint", "int", "integer", "bigint", "long", "short", "byte", "hugeint", "int2", "int4", "int8", "ubigint", "uinteger", "usmallint", "utinyint")
_FLOAT = ("double", "float", "real", "float4", "float8", "double precision")
_DECIMAL = ("decimal", "numeric", "number", "dec")
_STRING = ("string", "varchar", "char", "text", "character", "character varying", "nvarchar", "bpchar")
_DATE = ("date",)
_TIMESTAMP = ("timestamp", "timestamp_ntz", "timestamp_ltz", "timestamp with time zone", "timestamp without time zone", "datetime", "timestamptz")
_BOOLEAN = ("boolean", "bool")


def type_family(type_name: str) -> str:
    """Collapse an engine type name into one of: integer, float, decimal, string, date,
    timestamp, boolean, other. Parameters such as ``(7,2)`` are ignored."""
    base = type_name.strip().lower().split("(", 1)[0].strip()
    if base in _INTEGER:
        return "integer"
    if base in _FLOAT:
        return "float"
    if base in _DECIMAL:
        return "decimal"
    if base in _STRING:
        return "string"
    if base in _DATE:
        return "date"
    if base in _TIMESTAMP:
        return "timestamp"
    if base in _BOOLEAN:
        return "boolean"
    return "other"


def decimal_scale(type_name: str) -> int | None:
    """``decimal(7,2)`` -> 2; ``decimal(7)`` -> 0; anything else -> None."""
    t = type_name.strip().lower()
    if type_family(t) != "decimal" or "(" not in t:
        return None
    inner = t.split("(", 1)[1].rstrip(")")
    parts = [p.strip() for p in inner.split(",")]
    if len(parts) == 1:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return None
