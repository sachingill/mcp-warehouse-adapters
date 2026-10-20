"""SnowflakeWarehouse: implements the cpg-analytics-mcp `Warehouse` interface on Snowflake.

Read-only is enforced by the platform role (SELECT grants only), not by this code. Every statement
carries a query tag and runs under a session statement timeout.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import snowflake.connector
from cpg_analytics_mcp.warehouse import QueryResult, QueryTimeout, cap_rows
from snowflake.connector.errors import ProgrammingError

from mcp_warehouse_adapters.snowflake.config import SnowflakeConfig

if TYPE_CHECKING:
    from cpg_analytics_mcp.config import Settings

# 000630: statement reached its statement timeout; 000604: statement was cancelled.
TIMEOUT_ERRNOS = {604, 630}

CATALOG_SQL = """
SELECT c.table_name, t.table_type, c.column_name, c.data_type
FROM information_schema.columns c
JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = CURRENT_SCHEMA()
ORDER BY c.table_name, c.ordinal_position
"""


def plain(value: Any) -> Any:
    """Driver values -> plain Python, as the Warehouse contract requires (no Decimal)."""
    if isinstance(value, Decimal):
        return int(value) if value.as_tuple().exponent >= 0 else float(value)
    return value


class SnowflakeWarehouse:
    dialect = "snowflake"

    def __init__(self, config: SnowflakeConfig, timeout_s: float, query_tag: dict | None = None):
        self.timeout_s = max(1, math.ceil(timeout_s))  # Snowflake timeouts are whole seconds
        self._con = snowflake.connector.connect(
            **config.connect_args(),
            paramstyle="qmark",  # bind `?` placeholders exactly as core writes them
            session_parameters={
                "QUERY_TAG": json.dumps(query_tag or {"app": "cpg-analytics-mcp"}),
                "STATEMENT_TIMEOUT_IN_SECONDS": self.timeout_s,
            },
        )
        # Only the primary role may count. New users default to DEFAULT_SECONDARY_ROLES = ('ALL'), which
        # silently adds every granted role's privileges (a "sales" session could read finance objects).
        with self._con.cursor() as cur:
            cur.execute("USE SECONDARY ROLES NONE")

    def catalog(self) -> dict[str, dict]:
        """Only objects the platform role can see appear here, so the catalog reflects Snowflake grants."""
        out: dict[str, dict] = {}
        with self._con.cursor() as cur:
            for table, ttype, col, dtype in cur.execute(CATALOG_SQL).fetchall():
                entry = out.setdefault(table.lower(), {"type": "view" if ttype == "VIEW" else "table",
                                                       "columns": {}})
                entry["columns"][col.lower()] = dtype
        return out

    def query(self, sql: str, params: list | None = None, max_rows: int = 200) -> QueryResult:
        with self._con.cursor() as cur:
            try:
                cur.execute(cap_rows(sql, max_rows), params or None)
            except ProgrammingError as e:
                if e.errno in TIMEOUT_ERRNOS:
                    raise QueryTimeout(f"Query exceeded {self.timeout_s}s timeout.") from e
                raise
            columns = [d.name.lower() for d in cur.description]  # Snowflake upper-cases identifiers
            rows = [[plain(v) for v in row] for row in cur.fetchall()]
        truncated = len(rows) > max_rows
        return QueryResult(columns, rows[:max_rows], truncated)

    def close(self) -> None:
        self._con.close()


def factory(settings: Settings) -> SnowflakeWarehouse:
    """Entry point for `CPG_MCP_WAREHOUSE=snowflake`. The server role selects the Snowflake role."""
    config = SnowflakeConfig.from_env(settings.role)
    return SnowflakeWarehouse(config, settings.query_timeout_s,
                              query_tag={"app": "cpg-analytics-mcp", "mcp_role": settings.role})
