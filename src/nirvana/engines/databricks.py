"""Databricks SQL warehouse through the SQL Statement Execution API (``/api/2.0/sql/statements``).

Works with the free Databricks edition: one serverless SQL warehouse and a personal access
token. Results come back as ``JSON_ARRAY`` (every value a string or null) with the column
types in the manifest, so values are typed from ``type_text`` before comparison.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..model import EngineError, ResultSet
from .base import Engine, Timer, columns_from_names

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


class DatabricksEngine(Engine):
    name = "databricks"
    dialect = "databricks"

    def __init__(
        self,
        host: str,
        token: str,
        warehouse_id: str,
        location: str = "samples.tpcds_sf1",
        *,
        transport: httpx.BaseTransport | None = None,
        poll_seconds: float = 1.0,
        timeout_seconds: float = 900.0,
    ) -> None:
        super().__init__(location)
        if not host or not token or not warehouse_id:
            raise ValueError("Databricks needs host, token and warehouse id")
        host = host.strip().rstrip("/")
        if not host.startswith("http"):
            host = "https://" + host
        self.host = host
        self.warehouse_id = warehouse_id
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._http = httpx.Client(
            base_url=host,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "nirvana/0.1"},
            timeout=httpx.Timeout(120.0),
            transport=transport,
        )

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "host": self.host, "warehouse_id": self.warehouse_id}

    def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise EngineError(self.name, f"cannot reach {self.host}: {e}") from e
        if r.status_code in (401, 403):
            raise EngineError(self.name, f"HTTP {r.status_code}: the token was refused")
        if r.status_code >= 400:
            raise EngineError(self.name, f"HTTP {r.status_code}: {r.text[:400]}")
        return r.json()

    def run(self, sql: str, *, max_rows: int = 100_000) -> ResultSet:
        body = {
            "statement": sql,
            "warehouse_id": self.warehouse_id,
            "wait_timeout": "50s",
            "on_wait_timeout": "CONTINUE",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "row_limit": max_rows + 1,
        }
        with Timer() as t:
            resp = self._json("POST", "/api/2.0/sql/statements", json=body)
            statement_id = resp.get("statement_id")
            if not statement_id and resp.get("status", {}).get("state") not in TERMINAL:
                raise EngineError(self.name, "the warehouse returned no statement id", sql=sql)
            deadline = time.monotonic() + self.timeout_seconds
            while resp.get("status", {}).get("state") not in TERMINAL:
                if time.monotonic() > deadline:
                    if statement_id:
                        self._json("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                    raise EngineError(self.name, f"statement did not finish in {self.timeout_seconds:.0f} s", sql=sql)
                time.sleep(self.poll_seconds)
                resp = self._json("GET", f"/api/2.0/sql/statements/{statement_id}")
            status = resp.get("status", {})
            if status.get("state") != "SUCCEEDED":
                err = status.get("error", {})
                raise EngineError(self.name, f"{status.get('state')}: {err.get('message', 'no message')}", sql=sql)
            rows: list[list[Any]] = list(resp.get("result", {}).get("data_array") or [])
            link = resp.get("result", {}).get("next_chunk_internal_link")
            while link and len(rows) <= max_rows:
                chunk = self._json("GET", link)
                rows.extend(chunk.get("data_array") or [])
                link = chunk.get("next_chunk_internal_link")
        cols = resp.get("manifest", {}).get("schema", {}).get("columns", [])
        cols = sorted(cols, key=lambda c: c.get("position", 0))
        names = [c["name"] for c in cols]
        types = [c.get("type_text") or c.get("type_name") or "string" for c in cols]
        truncated = len(rows) > max_rows or bool(resp.get("manifest", {}).get("truncated"))
        rows = rows[:max_rows]
        return ResultSet.build(
            self.name,
            sql,
            columns_from_names(names, rows, types),
            rows,
            elapsed_ms=t.ms,
            truncated=truncated,
            meta={"statement_id": statement_id},
        )
