"""Load the synthetic CPG dataset into Unity Catalog and push governance down.

    uv run mwa-databricks-load

Runs as the workspace admin. DuckDB -> Parquet -> UC Volume -> tables in cpg_mcp.raw (owners only); then
dynamic views and the analysis views in cpg_mcp.main, and grants on main only. Idempotent.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import duckdb
import sqlglot
from cpg_analytics_mcp.data.generate import VIEWS, build
from databricks import sql
from databricks.sdk import WorkspaceClient

from mcp_warehouse_adapters.databricks.config import DatabricksConfig, secret_keys, settings
from mcp_warehouse_adapters.databricks.governance import (
    RAW_SCHEMA,
    analysis_view,
    dynamic_view_statements,
    grant_statements,
)
from mcp_warehouse_adapters.policy import export_sql, load_roles


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def objects_in(cur, catalog: str, schema: str) -> dict[str, str]:
    cur.execute(f"SELECT table_name, table_type FROM {catalog}.information_schema.tables "
                f"WHERE table_schema = '{schema}'")
    return {name.lower(): "view" if kind == "VIEW" else "table" for name, kind in cur.fetchall()}


def main() -> None:
    cfg = DatabricksConfig.admin()
    catalog, schema = cfg.catalog, cfg.schema
    volume = f"/Volumes/{catalog}/{RAW_SCHEMA}/staging"
    w = WorkspaceClient(host=cfg.host, token=cfg.token)
    args = {k: v for k, v in cfg.connect_args().items() if k not in ("catalog", "schema")}  # schemas may not exist
    with sql.connect(**args) as con, con.cursor() as cur:
        for s in (RAW_SCHEMA, schema):
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{s}")
        cur.execute(f"CREATE VOLUME IF NOT EXISTS {catalog}.{RAW_SCHEMA}.staging")

        # 1. base tables in the protected raw layer
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cpg.duckdb"
            counts = build(path)  # same deterministic data as the DuckDB build (SEED=42)
            src = duckdb.connect(str(path), read_only=True)
            for table in counts:
                columns = [(row[0], row[1]) for row in src.execute(f"DESCRIBE {table}").fetchall()]
                parquet = Path(tmp) / f"{table}.parquet"
                src.execute(export_sql(table, columns, parquet))
                with parquet.open("rb") as f:
                    w.files.upload(f"{volume}/{table}.parquet", f, overwrite=True)
                cur.execute(f"CREATE OR REPLACE TABLE {catalog}.{RAW_SCHEMA}.{table} AS "
                            f"SELECT * FROM parquet.`{volume}/{table}.parquet`")
                log(f"loaded raw.{table:<16} {counts[table]:>7,} rows")
            src.close()

        # 2. migrate: earlier versions kept tables (and a column mask) in the exposed schema
        for name, kind in objects_in(cur, catalog, schema).items():
            if kind == "table":
                cur.execute(f"DROP TABLE {catalog}.{schema}.{name}")
        cur.execute(f"DROP FUNCTION IF EXISTS {catalog}.{schema}.mask_cogs_per_unit")

        # 3. dynamic views over raw, blocked columns hidden per user (from roles.yaml)
        roles = load_roles()
        cur.execute(f"SELECT table_name, column_name, data_type FROM {catalog}.information_schema.columns "
                    f"WHERE table_schema = '{RAW_SCHEMA}' ORDER BY table_name, ordinal_position")
        raw_columns: dict[str, list[str]] = {}
        column_types: dict[str, dict[str, str]] = {}
        for table, col, dtype in cur.fetchall():
            raw_columns.setdefault(table.lower(), []).append(col.lower())
            column_types.setdefault(table.lower(), {})[col.lower()] = dtype
        for stmt in dynamic_view_statements(roles, raw_columns, column_types, catalog, schema):
            cur.execute(stmt)
        log(f"created {len(raw_columns)} dynamic views in {schema} (blocked columns hidden per user)")

        # 4. analysis views read raw with the owner's rights, so governed metrics use real cost
        for stmt in sqlglot.transpile(VIEWS, read="duckdb", write="databricks"):
            cur.execute(analysis_view(stmt, catalog, schema))
        log("created 3 analysis views (translated from DuckDB with sqlglot, reading raw)")

        # 5. grants on the exposed schema only; nothing on raw
        env = settings()
        grantees = {role: env[secret_keys(role)[0]] for role in roles}  # application ids from setup
        grants = grant_statements(roles, objects_in(cur, catalog, schema), catalog, schema, grantees)
        for stmt in grants:
            cur.execute(stmt)
        log(f"applied {len(grants)} grants from roles.yaml (none on {RAW_SCHEMA})")


if __name__ == "__main__":
    main()
