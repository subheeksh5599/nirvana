"""Upload the dsdgen TPC-DS parquet files to a Unity Catalog volume and create one Delta
table per file, so Databricks holds exactly the bytes Zetaris reads."""

import pathlib
import sys

import httpx

from nirvana.config import load_env_file, settings
from nirvana.engines import build
from nirvana.workload import TPCDS_TABLES

SRC = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "~/nirvana-data/tpcds_sf1").expanduser()
CATALOG, SCHEMA, VOLUME = "workspace", "nirvana_tpcds", "files"

load_env_file()
s = settings()
e = build("databricks", s, role="source")
e.run(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
e.run(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.{VOLUME}")
host = s.databricks_host.rstrip("/")
headers = {"Authorization": f"Bearer {s.databricks_token}"}
with httpx.Client(timeout=900) as c:
    for t in sorted(TPCDS_TABLES):
        f = SRC / f"{t}.parquet"
        path = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}/{t}.parquet"
        with f.open("rb") as fh:
            r = c.put(f"{host}/api/2.0/fs/files{path}?overwrite=true", headers=headers, content=fh)
        r.raise_for_status()
        e.run(f"CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.{t} AS SELECT * FROM parquet.`{path}`")
        print("loaded", t, f.stat().st_size, flush=True)
sql = " UNION ALL ".join(f"SELECT '{t}' AS t, COUNT(*) AS n FROM {CATALOG}.{SCHEMA}.{t}" for t in sorted(TPCDS_TABLES))
for r in e.run(sql).rows:
    print(r[0], r[1])
