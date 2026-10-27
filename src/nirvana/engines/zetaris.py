"""Zetaris Lightning through its REST SQL endpoint (``POST /sql-editor/sqls/run``).

The request shape follows the Zetaris getting-started repository: a bearer API key created
in the Zetaris GUI, the numeric organisation id in ``X-Org-ID``, a UUID ``X-Request-ID`` per
call. The response puts rows under ``data``; column names and types are read from whichever
of ``columns`` / ``schema`` / ``metadata`` / ``header`` the instance returns, and types are
inferred from the JSON values only when the instance gives none (recorded in ``meta``).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from ..model import EngineError, ResultSet
from .base import Engine, Timer, columns_from_names

DEFAULT_REST_URL = "http://localhost:8888/api/v1.0"


def _column_specs(payload: dict[str, Any]) -> tuple[list[str] | None, list[str] | None]:
    for key in ("headers", "columns", "schema", "metadata", "header", "columnNames", "fields"):
        spec = payload.get(key)
        if isinstance(spec, dict) and isinstance(spec.get("fields"), list):
            spec = spec["fields"]
        if isinstance(spec, list) and spec:
            if all(isinstance(c, str) for c in spec):
                return list(spec), None
            if all(isinstance(c, dict) for c in spec):
                names = [str(c.get("name") or c.get("columnName") or c.get("label") or f"col{i}") for i, c in enumerate(spec)]
                types = [c.get("type") or c.get("dataType") or c.get("typeName") or c.get("columnType") for c in spec]
                if all(isinstance(t, str) and t for t in types):
                    return names, [str(t) for t in types]
                return names, None
    return None, None


def parse_payload(payload: Any) -> tuple[list[str], list[str] | None, list[list[Any]]]:
    """(names, types or None, rows) from a Zetaris run response."""
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    if payload.get("error") or payload.get("errorMessage"):
        raise ValueError(str(payload.get("error") or payload.get("errorMessage")))
    data = payload.get("data")
    if isinstance(data, dict):
        inner_names, inner_types = _column_specs(data)
        rows_raw = data.get("rows") or data.get("data") or []
        names, types = inner_names, inner_types
        if names is None:
            names, types = _column_specs(payload)
    else:
        rows_raw = data or []
        names, types = _column_specs(payload)
    if rows_raw and isinstance(rows_raw[0], dict):
        if names is None:
            names = list(rows_raw[0].keys())
        rows = [[r.get(n) for n in names] for r in rows_raw]
    else:
        rows = [list(r) for r in rows_raw]
        if names is None:
            width = len(rows[0]) if rows else 0
            names = [f"col{i}" for i in range(width)]
    return names, types, rows


class ZetarisEngine(Engine):
    name = "zetaris"
    dialect = "spark"

    def __init__(
        self,
        api_key: str,
        org_id: str = "1",
        rest_url: str = DEFAULT_REST_URL,
        location: str = "TPCDS_DB",
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 900.0,
        infer_types: bool = True,
    ) -> None:
        super().__init__(location)
        self.infer_types = infer_types
        if not api_key:
            raise ValueError("Zetaris needs an API key (create one in the Zetaris GUI)")
        if not str(org_id).isdigit():
            raise ValueError("ZETARIS_ORG_ID must be the numeric organisation id")
        self.rest_url = rest_url.rstrip("/")
        self.org_id = str(org_id)
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "X-Org-ID": self.org_id, "User-Agent": "nirvana/0.1"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "rest_url": self.rest_url}

    def run(self, sql: str, *, max_rows: int = 100_000) -> ResultSet:
        body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": max_rows + 1}
        with Timer() as t:
            try:
                r = self._http.post(
                    self.rest_url + "/sql-editor/sqls/run",
                    json=body,
                    headers={"X-Request-ID": str(uuid.uuid4())},
                )
            except httpx.HTTPError as e:
                raise EngineError(self.name, f"cannot reach {self.rest_url}: {e}", sql=sql) from e
        if r.status_code == 401:
            raise EngineError(self.name, "HTTP 401: the API key was refused", sql=sql)
        if r.status_code == 400 and "org" in r.text.lower():
            raise EngineError(self.name, "HTTP 400: check ZETARIS_ORG_ID", sql=sql)
        if r.status_code >= 400:
            raise EngineError(self.name, f"HTTP {r.status_code}: {r.text[:400]}", sql=sql)
        try:
            names, types, rows = parse_payload(r.json())
        except ValueError as e:
            raise EngineError(self.name, str(e), sql=sql) from e
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        type_source = "response"
        if types is None and names and self.infer_types and not sql.lstrip().upper().startswith("INFER "):
            types = self.output_types(sql, len(names))
            type_source = "infer_sqltable" if types is not None else "values"
        elif types is None:
            type_source = "values"
        return ResultSet.build(
            self.name,
            sql,
            columns_from_names(names, rows, types),
            rows,
            elapsed_ms=t.ms,
            truncated=truncated,
            meta={"types_inferred": type_source == "values", "type_source": type_source},
        )

    def output_types(self, sql: str, width: int) -> list[str] | None:
        """Output column types from Lightning's own ``INFER SQLTABLE COLUMNS SPEC <query>``,
        used when the SQL endpoint returns rows without types. None when it is not available
        (it needs the Data Pipeline Write privilege) or does not return one type per column."""
        try:
            rs = self.run("INFER SQLTABLE COLUMNS SPEC " + sql.strip().rstrip(";"), max_rows=10_000)
        except EngineError:
            return None
        lower = [c.name.lower() for c in rs.columns]
        t_idx = next((i for i, n in enumerate(lower) if "type" in n), None)
        if t_idx is None:
            return None
        found = [str(r[t_idx]).strip().strip('"').strip().lower() for r in rs.rows if r[t_idx] is not None]
        return found if len(found) == width else None
