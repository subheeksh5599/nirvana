"""DuckDB as a real local engine.

Used for rehearsals without credentials and by the test suite. ``tpcds_scale`` generates
TPC-DS data in-process with DuckDB's own generator, so every query runs on real rows.
``load_as`` lets a rehearsal copy the data with a different column type (for example every
DECIMAL as DOUBLE, which is what a CSV loaded with type inference does) to reproduce a class
of migration drift on a second engine.
"""

from __future__ import annotations

import threading
from typing import Any

import duckdb
import sqlglot

from ..model import EngineError, ResultSet
from .base import Engine, Timer, columns_from_names


class DuckDBEngine(Engine):
    dialect = "duckdb"

    def __init__(
        self,
        name: str = "duckdb",
        location: str = "main",
        *,
        database: str = ":memory:",
        read_dialect: str = "spark",
        connection: duckdb.DuckDBPyConnection | None = None,
    ) -> None:
        super().__init__(location)
        self.name = name
        self.read_dialect = read_dialect
        # Text read as Spark is translated with Spark's NULL placement; raw DuckDB text sorts
        # NULLs last in both directions unless the session says otherwise.
        self.null_order = "spark" if read_dialect == "spark" else "last"
        self.con = connection or duckdb.connect(database)
        self._lock = threading.Lock()

    @classmethod
    def tpcds(cls, name: str = "duckdb", scale: float = 0.01) -> "DuckDBEngine":
        eng = cls(name)
        eng.con.execute("INSTALL tpcds; LOAD tpcds")
        eng.con.execute(f"CALL dsdgen(sf={float(scale)})")
        return eng

    def clone_with_types(self, name: str, decimal_as: str | None = None, schema: str = "drift") -> "DuckDBEngine":
        """A second engine over a copy of every table in its own schema of the same database;
        DECIMAL columns retyped when asked. It shares this engine's connection and lock."""
        self.con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        tables = [r[0] for r in self.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?", [self.location]
        ).fetchall()]
        for t in tables:
            cols = self.con.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                [self.location, t],
            ).fetchall()
            select = []
            for col, typ in cols:
                if decimal_as and typ.upper().startswith("DECIMAL"):
                    select.append(f'CAST("{col}" AS {decimal_as}) AS "{col}"')
                else:
                    select.append(f'"{col}"')
            self.con.execute(f'CREATE OR REPLACE TABLE "{schema}"."{t}" AS SELECT {", ".join(select)} FROM "{self.location}"."{t}"')
        other = DuckDBEngine(name, schema, connection=self.con, read_dialect=self.read_dialect)
        other._lock = self._lock
        return other

    def translate(self, sql: str) -> str:
        if self.read_dialect == "duckdb":
            return sql
        return sqlglot.transpile(sql, read=self.read_dialect, write="duckdb")[0]

    def run(self, sql: str, *, max_rows: int = 100_000) -> ResultSet:
        try:
            text = self.translate(sql)
        except sqlglot.errors.ParseError as e:
            raise EngineError(self.name, f"cannot translate: {e}", sql=sql) from e
        with self._lock, Timer() as t:
            try:
                cur = self.con.execute(text)
                rows: list[Any] = cur.fetchmany(max_rows + 1)
                desc = cur.description or []
            except duckdb.Error as e:
                raise EngineError(self.name, str(e).splitlines()[0], sql=sql) from e
        names = [d[0] for d in desc]
        types = [str(d[1]) for d in desc]
        truncated = len(rows) > max_rows
        rows = [list(r) for r in rows[:max_rows]]
        return ResultSet.build(self.name, sql, columns_from_names(names, rows, types), rows, elapsed_ms=t.ms, truncated=truncated)
