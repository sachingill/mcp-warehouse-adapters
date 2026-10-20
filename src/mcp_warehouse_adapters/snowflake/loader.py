"""Load the synthetic CPG dataset into Snowflake, create the views, and push governance down.

    uv run mwa-snowflake-load

Runs as CPG_LOADER. Standard bulk load: DuckDB -> Parquet -> PUT to the table stage -> COPY INTO.
Table DDL and view SQL are translated from DuckDB with sqlglot. Rebuilds tables each time
(deterministic data), so grants and masks are re-applied.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import duckdb
import snowflake.connector
import sqlglot
from cpg_analytics_mcp.data.generate import VIEWS, build

from mcp_warehouse_adapters.policy import export_sql
from mcp_warehouse_adapters.snowflake.config import SnowflakeConfig
from mcp_warehouse_adapters.snowflake.governance import (
    LOADER_ROLE,
    grant_statements,
    load_roles,
    masking_plans,
)

OBJECTS_SQL = "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = CURRENT_SCHEMA()"
COLUMNS_SQL = """
SELECT c.table_name, c.column_name, c.data_type
FROM information_schema.columns c
JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = CURRENT_SCHEMA() AND t.table_type = 'BASE TABLE'
"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _portable(duckdb_type: str) -> str:
    return "TIMESTAMP" if duckdb_type.startswith("TIMESTAMP") else duckdb_type  # TIMESTAMP_S/_NS -> TIMESTAMP


def snowflake_ddl(table: str, columns: list[tuple[str, str]]) -> str:
    """CREATE TABLE in Snowflake SQL from DuckDB (name, type) pairs."""
    cols = ", ".join(f"{name} {_portable(ty)}" for name, ty in columns)
    return sqlglot.transpile(f"CREATE OR REPLACE TABLE {table} ({cols})", read="duckdb", write="snowflake")[0]


def main() -> None:
    config = SnowflakeConfig.from_env("loader", role_override=LOADER_ROLE)
    con = snowflake.connector.connect(**config.connect_args())
    cur = con.cursor()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cpg.duckdb"
            counts = build(path)  # same deterministic data as the DuckDB build (SEED=42)
            src = duckdb.connect(str(path), read_only=True)
            for table in counts:
                columns = [(row[0], row[1]) for row in src.execute(f"DESCRIBE {table}").fetchall()]
                parquet = Path(tmp) / f"{table}.parquet"
                src.execute(export_sql(table, columns, parquet))
                cur.execute(snowflake_ddl(table, columns))
                cur.execute(f"PUT 'file://{parquet}' @%{table} AUTO_COMPRESS = FALSE OVERWRITE = TRUE")
                cur.execute(f"COPY INTO {table} FROM @%{table} "
                            "FILE_FORMAT = (TYPE = PARQUET USE_LOGICAL_TYPE = TRUE) "
                            "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE PURGE = TRUE")
                log(f"loaded {table:<16} {counts[table]:>7,} rows")
            src.close()

        for stmt in sqlglot.transpile(VIEWS, read="duckdb", write="snowflake"):
            cur.execute(stmt)
        log("created 3 views (translated from DuckDB with sqlglot)")

        roles = load_roles()
        objects = {name.lower(): "view" if kind == "VIEW" else "table"
                   for name, kind in cur.execute(OBJECTS_SQL).fetchall()}
        base_columns: dict[str, dict[str, str]] = {}
        for table, col, dtype in cur.execute(COLUMNS_SQL).fetchall():
            base_columns.setdefault(table.lower(), {})[col.lower()] = dtype

        for plan in masking_plans(roles, base_columns):
            for stmt in plan.statements():
                cur.execute(stmt)
            log(f"masked {plan.table}.{plan.column}; visible to {list(plan.allowed_roles)}")
        grants = grant_statements(roles, objects)
        for stmt in grants:
            cur.execute(stmt)
        log(f"applied {len(grants)} SELECT grants from roles.yaml")
    finally:
        cur.close()
        con.close()


if __name__ == "__main__":
    main()
