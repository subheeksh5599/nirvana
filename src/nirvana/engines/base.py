"""Engines run SQL and hand back a ResultSet. Each knows its SQL dialect and where its
copy of the workload tables lives."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..model import Column, EngineError, ResultSet


@dataclass(frozen=True)
class EngineStatus:
    engine: str
    ok: bool
    detail: str


class Engine(ABC):
    name: str = "engine"
    dialect: str = "spark"

    def __init__(self, location: str) -> None:
        self.location = location

    @abstractmethod
    def run(self, sql: str, *, max_rows: int = 100_000) -> ResultSet: ...

    def ping(self) -> EngineStatus:
        try:
            rs = self.run("SELECT 1 AS one", max_rows=1)
        except EngineError as e:
            return EngineStatus(self.name, False, e.message)
        ok = rs.row_count == 1
        return EngineStatus(self.name, ok, "SELECT 1 returned one row" if ok else "SELECT 1 returned no rows")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "dialect": self.dialect, "location": self.location}


def infer_type(values: list[Any]) -> str:
    """A type name for a column whose engine reported no types: from the JSON values."""
    seen = {type(v) for v in values if v is not None}
    if not seen:
        return "string"
    if seen <= {bool}:
        return "boolean"
    if seen <= {int}:
        return "bigint"
    if seen <= {int, float}:
        return "double"
    return "string"


class Timer:
    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self.start) * 1000


def columns_from_names(names: list[str], rows: list[list[Any]], types: list[str] | None) -> list[Column]:
    if types is not None and len(types) != len(names):
        raise ValueError("types and names differ in length")
    out = []
    for i, n in enumerate(names):
        t = types[i] if types else infer_type([r[i] for r in rows if i < len(r)])
        out.append(Column(str(n), str(t).lower()))
    return out
