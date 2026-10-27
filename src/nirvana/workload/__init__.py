"""The workload: named SQL queries, and how to point them at one engine's copy of the data.

The built-in workload is the 103 TPC-DS query texts as Apache Spark renders them (see
``tpcds/NOTICE.md``). A query names tables without a schema (``store_sales``); each engine
keeps its copy somewhere else (``samples.tpcds_sf1.store_sales`` on Databricks,
``TPCDS_DB.store_sales`` on Zetaris), so ``qualify`` rewrites only those table references
and leaves CTE names, aliases and columns alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

import sqlglot
from sqlglot import exp

TPCDS_TABLES = frozenset(
    {
        "call_center", "catalog_page", "catalog_returns", "catalog_sales", "customer",
        "customer_address", "customer_demographics", "date_dim", "household_demographics",
        "income_band", "inventory", "item", "promotion", "reason", "ship_mode", "store",
        "store_returns", "store_sales", "time_dim", "warehouse", "web_page", "web_returns",
        "web_sales", "web_site",
    }
)

READ_DIALECT = "spark"


@dataclass(frozen=True)
class Query:
    id: str  # "q1", "q14a", ...
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def number(self) -> tuple[int, str]:
        m = re.fullmatch(r"q(\d+)([a-z]?)", self.id)
        if not m:
            return (10**6, self.id)
        return (int(m.group(1)), m.group(2))


@cache
def tpcds() -> tuple[Query, ...]:
    """All TPC-DS queries in numeric order (q1, q2, ..., q14a, q14b, ..., q99)."""
    folder = resources.files("nirvana.workload").joinpath("tpcds")
    out = []
    for entry in folder.iterdir():
        name = entry.name
        if name.endswith(".sql"):
            out.append(Query(name[:-4], entry.read_text(encoding="utf-8").strip()))
    return tuple(sorted(out, key=lambda q: q.number))


def get(query_id: str) -> Query:
    for q in tpcds():
        if q.id == query_id:
            return q
    raise KeyError(f"no query {query_id!r} in the workload")


def select(ids: list[str] | None) -> list[Query]:
    if not ids:
        return list(tpcds())
    return [get(i) for i in ids]


def parse(sql: str, dialect: str = READ_DIALECT) -> exp.Expression:
    tree = sqlglot.parse_one(sql, read=dialect)
    if tree is None:
        raise ValueError("empty statement")
    return tree


def tables_read(sql: str) -> list[str]:
    """Workload tables a query reads (lower-case, sorted, no CTE names)."""
    tree = parse(sql)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    names = set()
    for t in tree.find_all(exp.Table):
        n = t.name.lower()
        if n and n not in ctes and n in TPCDS_TABLES:
            names.add(n)
    return sorted(names)


def has_top_level_order(sql: str) -> bool:
    """True when the outermost statement sorts its output, so row order is part of the answer."""
    tree = parse(sql)
    return tree.args.get("order") is not None


def top_level_limit(sql: str) -> int | None:
    tree = parse(sql)
    limit = tree.args.get("limit")
    if limit is None:
        return None
    value = limit.expression if isinstance(limit, exp.Limit) else None
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.this)
    return None


def split_location(location: str) -> tuple[str | None, str]:
    """``samples.tpcds_sf1`` -> ("samples", "tpcds_sf1"); ``TPCDS_DB`` -> (None, "TPCDS_DB")."""
    parts = [p for p in location.strip().split(".") if p]
    if not parts or len(parts) > 2:
        raise ValueError(f"table location must be 'schema' or 'catalog.schema', got {location!r}")
    if len(parts) == 1:
        return None, parts[0]
    return parts[0], parts[1]


def qualify(sql: str, location: str, write_dialect: str = READ_DIALECT) -> str:
    """Point every workload table at ``location`` and render for ``write_dialect``."""
    catalog, schema = split_location(location)
    tree = parse(sql)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        n = t.name.lower()
        if n in ctes or n not in TPCDS_TABLES or t.args.get("db") is not None:
            continue
        t.set("db", exp.to_identifier(schema))
        if catalog:
            t.set("catalog", exp.to_identifier(catalog))
    return tree.sql(dialect=write_dialect)


def unqualified_tables(sql: str, dialect: str = READ_DIALECT) -> list[str]:
    """Workload tables still referenced without a schema (should be empty after qualify)."""
    tree = parse(sql, dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return sorted(
        {
            t.name.lower()
            for t in tree.find_all(exp.Table)
            if t.name.lower() in TPCDS_TABLES and t.name.lower() not in ctes and t.args.get("db") is None
        }
    )
