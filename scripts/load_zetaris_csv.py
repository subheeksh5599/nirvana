"""Register CSV exports of the same TPC-DS tables on Zetaris with schema inference.

This is the common "export to CSV, load with inferred types" migration path: the rows are
the same as the parquet files, but the types are whatever inference decides (a DECIMAL(7,2)
becomes DOUBLE). The CSV files must be in the driver container under
/home/zetaris/customer-data/shared/nirvana_csv/<table>/<table>.csv."""

import sys

from nirvana.config import load_env_file, settings
from nirvana.engines import build
from nirvana.workload import TPCDS_TABLES

ROOT = "/home/zetaris/customer-data/shared/nirvana_csv"
DB = sys.argv[1] if len(sys.argv) > 1 else "NIRVANA_CSV"

load_env_file()
e = build("zetaris", settings(), role="target")
try:
    e.run(f'CREATE LIGHTNING DATABASE {DB} DESCRIBE BY "TPC-DS sf1 loaded from CSV exports with inferred types" OPTIONS ()')
except Exception as x:
    print("database:", str(x)[:120])
for t in sorted(TPCDS_TABLES):
    try:
        e.run(f'CREATE LIGHTNING FILESTORE TABLE {t} FROM {DB} FORMAT csv OPTIONS (path "{ROOT}/{t}", header "true", inferSchema "true")')
        print("registered", t, flush=True)
    except Exception as x:
        print("skip", t, str(x)[:160], flush=True)
