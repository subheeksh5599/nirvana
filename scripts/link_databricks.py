"""Register the Databricks SQL warehouse as a Zetaris datasource over JDBC, so Zetaris can read
Databricks tables in the same query as its own (the Databricks JDBC driver ships with Zetaris).

The token travels only inside the CREATE DATASOURCE statement sent to the local Zetaris; it is
never printed."""

from nirvana.config import load_env_file, settings
from nirvana.engines import build

load_env_file()
s = settings()
host = s.databricks_host.replace("https://", "").rstrip("/")
url = (f"jdbc:databricks://{host}:443/default;transportMode=http;ssl=1;"
       f"httpPath=/sql/1.0/warehouses/{s.databricks_warehouse_id};AuthMech=3;UID=token;PWD={s.databricks_token}")
z = build("zetaris", s, role="target")
try:
    z.run(f'CREATE DATASOURCE DATABRICKS DESCRIBE BY "Databricks SQL warehouse" OPTIONS (jdbcdriver "com.databricks.client.jdbc.Driver", jdbcurl "{url}", fetchsize "10000")')
    print("datasource DATABRICKS registered")
except Exception as e:
    print(str(e).replace(s.databricks_token, "***")[:300])
print(z.run(f"PREVIEW DATASOURCE SQL DATABRICKS AS SELECT COUNT(*) FROM {s.source_location}.store").rows)
