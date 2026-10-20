"""Push cpg-analytics-mcp role policies (roles.yaml) down to Unity Catalog: a protected raw layer with
dynamic views on top.

    cpg_mcp.raw   base tables, real values, readable by owners only
    cpg_mcp.main  one view per table (blocked columns become CASE WHEN is_member(...) THEN col END),
                  plus the analysis views, which read raw.* with the view owner's rights

Why not column masks: Unity Catalog evaluates a mask as the querying user even inside a view, so an
analyst's promo ROI would be NULL. A dynamic view hides cost per user while approved views still
compute margin from real cost (the governed-metric pattern).

Grants go to each role's service principal (by application id). Free Edition only has workspace-local
groups, which Unity Catalog cannot grant to (PRINCIPAL_DOES_NOT_EXIST); in a full account, use account
groups for grants and is_account_group_member() in the views. Pure functions returning SQL.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from mcp_warehouse_adapters.databricks.config import group_name
from mcp_warehouse_adapters.policy import allowed_objects, mask_targets

RAW_SCHEMA = "raw"
OWNER_GROUP = "admins"  # the workspace admins who load data and own the views


def grant_statements(roles: dict[str, dict], objects: dict[str, str], catalog: str, schema: str,
                     grantees: dict[str, str]) -> list[str]:
    """Grants on the exposed schema only; nothing on raw. `grantees`: server role -> application id."""
    stmts = []
    for role, spec in roles.items():
        who = f"`{grantees[role]}`"
        stmts += [f"GRANT USE CATALOG ON CATALOG {catalog} TO {who}",
                  f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO {who}"]
        # Unity Catalog grants on views also use the TABLE securable type
        stmts += [f"GRANT SELECT ON TABLE {catalog}.{schema}.{name} TO {who}"
                  for name in allowed_objects(spec, objects)]
    return stmts


def dynamic_view_statements(roles: dict[str, dict], raw_columns: dict[str, list[str]],
                            column_types: dict[str, dict[str, str]], catalog: str, schema: str) -> list[str]:
    """One view per raw table. `raw_columns`: {table: [columns in order]}; blocked columns are hidden per user."""
    hidden = {(t.table, t.column): t.allowed_roles for t in mask_targets(roles, column_types)}
    stmts = []
    for table, cols in sorted(raw_columns.items()):
        select = []
        for col in cols:
            if (table, col) in hidden:
                who = " OR ".join([f"is_member('{group_name(r)}')" for r in hidden[(table, col)]]
                                  + [f"is_member('{OWNER_GROUP}')"])
                select.append(f"CASE WHEN {who} THEN {col} END AS {col}")
            else:
                select.append(col)
        stmts.append(f"CREATE OR REPLACE VIEW {catalog}.{schema}.{table} AS "
                     f"SELECT {', '.join(select)} FROM {catalog}.{RAW_SCHEMA}.{table}")
    return stmts


def analysis_view(create_sql: str, catalog: str, schema: str) -> str:
    """Place an analysis view in the exposed schema, reading the raw tables (not the dynamic views)."""
    tree = sqlglot.parse_one(create_sql, read="databricks")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.expression.find_all(exp.Table):
        if not table.args.get("db") and table.name.lower() not in ctes:
            table.set("db", exp.to_identifier(RAW_SCHEMA))
            table.set("catalog", exp.to_identifier(catalog))
    target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
    target.set("db", exp.to_identifier(schema))
    target.set("catalog", exp.to_identifier(catalog))
    return tree.sql(dialect="databricks")
