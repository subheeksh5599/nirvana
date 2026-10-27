"""Register the dsdgen TPC-DS parquet files as Zetaris Lightning filestore tables.

The files must already be copied into the driver container under
/home/zetaris/customer-data/shared/nirvana_tpcds/<table>/<table>.parquet."""

import sys

from nirvana.config import load_env_file, settings
from nirvana.engines import build
from nirvana.workload import TPCDS_TABLES

ROOT = "/home/zetaris/customer-data/shared/nirvana_tpcds"
DB = sys.argv[1] if len(sys.argv) > 1 else "NIRVANA_TPCDS"

load_env_file()
e = build("zetaris", settings(), role="target")
for t in sorted(TPCDS_TABLES):
    try:
        e.run(f'CREATE LIGHTNING FILESTORE TABLE {t} FROM {DB} FORMAT parquet OPTIONS (path "{ROOT}/{t}")')
        print("registered", t, flush=True)
    except Exception as x:
        print("skip", t, str(x)[:120], flush=True)
sql = " UNION ALL ".join(f"SELECT '{t}' AS t, COUNT(*) AS n FROM {DB}.{t}" for t in sorted(TPCDS_TABLES))
for r in e.run(sql).rows:
    print(r[0], r[1])
