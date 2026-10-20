"""DatabricksWarehouse: implements the cpg-analytics-mcp `Warehouse` interface on a Databricks SQL warehouse.

Read-only is enforced by Unity Catalog grants on the role's service principal, not by this code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from cpg_analytics_mcp.warehouse import QueryResult, QueryTimeout, cap_rows
from databricks import sql
from sqlglot import exp

from mcp_warehouse_adapters.databricks.config import DatabricksConfig
from mcp_warehouse_adapters.policy import plain

if TYPE_CHECKING:
    from cpg_analytics_mcp.config import Settings

CATALOG_SQL = """
SELECT c.table_name, t.table_type, c.column_name, c.data_type
FROM information_schema.columns c
JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = current_schema()
ORDER BY c.table_name, c.ordinal_position
"""


def to_named(sql_text: str, params: list | None) -> tuple[str, dict]:
    """Core writes `?` placeholders; the Databricks driver binds named ones. Rewrite on the parsed SQL,
    so a `?` inside a string literal is never touched."""
    if not params:
        return sql_text, {}
    names = iter(f"p{i}" for i in range(len(params)))

    def rename(node: exp.Expression) -> exp.Expression:
        return exp.Placeholder(this=next(names)) if isinstance(node, exp.Placeholder) and not node.this else node

    tree = sqlglot.parse_one(sql_text, read="databricks").transform(rename)
    return tree.sql(dialect="databricks"), {f"p{i}": v for i, v in enumerate(params)}


def is_timeout(error: Exception) -> bool:
    text = str(error).upper()
    return "STATEMENT_TIMEOUT" in text or "TIMED OUT" in text or ("TIMEOUT" in text and "CANCEL" in text)


class DatabricksWarehouse:
    dialect = "databricks"

    def __init__(self, config: DatabricksConfig, timeout_s: float):
        self.timeout_s = max(1, round(timeout_s))  # whole seconds
        self._con = sql.connect(**config.connect_args(),
                                session_configuration={"STATEMENT_TIMEOUT": str(self.timeout_s)})

    def catalog(self) -> dict[str, dict]:
        """Unity Catalog only shows objects the identity can access, so this reflects platform grants."""
        out: dict[str, dict] = {}
        with self._con.cursor() as cur:
            cur.execute(CATALOG_SQL)
            for table, ttype, col, dtype in cur.fetchall():
                entry = out.setdefault(table.lower(), {"type": "view" if ttype == "VIEW" else "table",
                                                       "columns": {}})
                entry["columns"][col.lower()] = dtype
        return out

    def query(self, sql_text: str, params: list | None = None, max_rows: int = 200) -> QueryResult:
        statement, named = to_named(cap_rows(sql_text, max_rows), params)
        with self._con.cursor() as cur:
            try:
                cur.execute(statement, named or None)
            except sql.exc.Error as e:
                if is_timeout(e):
                    raise QueryTimeout(f"Query exceeded {self.timeout_s}s timeout.") from e
                raise
            columns = [d[0].lower() for d in cur.description]
            rows = [[plain(v) for v in row] for row in cur.fetchall()]
        truncated = len(rows) > max_rows
        return QueryResult(columns, rows[:max_rows], truncated)

    def close(self) -> None:
        self._con.close()


def factory(settings: Settings) -> DatabricksWarehouse:
    """Entry point for `CPG_MCP_WAREHOUSE=databricks`: log in as the server role's service principal."""
    return DatabricksWarehouse(DatabricksConfig.for_role(settings.role), settings.query_timeout_s)
