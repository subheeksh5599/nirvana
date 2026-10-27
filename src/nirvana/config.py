"""Settings come from the environment, optionally loaded from ``.env.local``.

Secrets are only ever read here and handed to the engine clients; nothing in Nirvana logs,
stores or returns them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = ".env.local"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    """Load ``.env.local`` from the given path, the working directory or the repo root."""
    candidates = [Path(path)] if path else [Path.cwd() / ENV_FILE, Path(__file__).resolve().parents[2] / ENV_FILE]
    for p in candidates:
        if p.is_file():
            for k, v in parse_env_text(p.read_text(encoding="utf-8")).items():
                if override or k not in os.environ:
                    os.environ[k] = v
            return p
    return None


SECRET_KEYS = ("DATABRICKS_TOKEN", "ZETARIS_API_KEY", "METERLESS_TOKEN", "NVIDIA_API_KEY")


@dataclass(frozen=True)
class Settings:
    home: Path
    source: str
    target: str
    source_location: str
    target_location: str
    databricks_host: str
    databricks_token: str
    databricks_warehouse_id: str
    zetaris_rest_url: str
    zetaris_api_key: str
    zetaris_org_id: str
    meterless_url: str
    meterless_token: str
    meterless_model: str
    nvidia_api_key: str
    rehearsal_scale: float
    max_rows: int

    @property
    def ledger_path(self) -> Path:
        return self.home / "ledger.sqlite"

    @property
    def recordings_dir(self) -> Path:
        return Path(os.environ.get("NIRVANA_RECORDINGS", str(Path(__file__).resolve().parents[2] / "recordings")))

    @property
    def memory_dir(self) -> Path:
        return self.home / "hmem"

    def configured(self) -> dict[str, bool]:
        """Which integrations have what they need (never the values)."""
        return {
            "databricks": bool(self.databricks_host and self.databricks_token and self.databricks_warehouse_id),
            "zetaris": bool(self.zetaris_api_key and self.zetaris_org_id),
            "meterless": bool(self.meterless_url or self.meterless_token),
            "nvidia": bool(self.nvidia_api_key),
        }


def settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env

    def g(key: str, default: str = "") -> str:
        return e.get(key, default).strip()

    home = Path(g("NIRVANA_HOME", str(Path(__file__).resolve().parents[2] / "var")))
    return Settings(
        home=home,
        source=g("NIRVANA_SOURCE", "databricks"),
        target=g("NIRVANA_TARGET", "zetaris"),
        source_location=g("NIRVANA_SOURCE_LOCATION", "samples.tpcds_sf1"),
        target_location=g("NIRVANA_TARGET_LOCATION", "TPCDS_DB"),
        databricks_host=g("DATABRICKS_HOST"),
        databricks_token=g("DATABRICKS_TOKEN"),
        databricks_warehouse_id=g("DATABRICKS_WAREHOUSE_ID"),
        zetaris_rest_url=g("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0"),
        zetaris_api_key=g("ZETARIS_API_KEY"),
        zetaris_org_id=g("ZETARIS_ORG_ID", "1"),
        meterless_url=g("METERLESS_URL"),
        meterless_token=g("METERLESS_TOKEN"),
        meterless_model=g("METERLESS_MODEL", "meterless/auto"),
        nvidia_api_key=g("NVIDIA_API_KEY"),
        rehearsal_scale=float(g("NIRVANA_REHEARSAL_SCALE", "0.01") or "0.01"),
        max_rows=int(g("NIRVANA_MAX_ROWS", "100000") or "100000"),
    )
