"""Build engines from settings."""

from __future__ import annotations

from ..config import Settings
from .base import Engine, EngineStatus
from .databricks import DatabricksEngine
from .duck import DuckDBEngine
from .zetaris import ZetarisEngine

__all__ = ["Engine", "EngineStatus", "DatabricksEngine", "DuckDBEngine", "ZetarisEngine", "build", "rehearsal_pair"]

_rehearsal: dict[float, tuple[DuckDBEngine, DuckDBEngine]] = {}


def rehearsal_pair(scale: float = 0.01) -> tuple[DuckDBEngine, DuckDBEngine]:
    """Two local engines over the same generated TPC-DS rows; the second keeps every
    DECIMAL as DOUBLE, the drift a CSV-with-type-inference load produces. Labelled as a
    rehearsal everywhere it is shown."""
    if scale not in _rehearsal:
        src = DuckDBEngine.tpcds("rehearsal-source", scale)
        tgt = src.clone_with_types("rehearsal-target", decimal_as="DOUBLE")
        _rehearsal[scale] = (src, tgt)
    return _rehearsal[scale]


def build(kind: str, s: Settings, *, role: str) -> Engine:
    location = s.source_location if role == "source" else s.target_location
    if kind == "databricks":
        return DatabricksEngine(s.databricks_host, s.databricks_token, s.databricks_warehouse_id, location)
    if kind == "zetaris":
        return ZetarisEngine(s.zetaris_api_key, s.zetaris_org_id, s.zetaris_rest_url, location)
    if kind == "rehearsal":
        src, tgt = rehearsal_pair(s.rehearsal_scale)
        return src if role == "source" else tgt
    raise ValueError(f"unknown engine {kind!r} (databricks, zetaris, rehearsal)")
