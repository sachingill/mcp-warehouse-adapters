"""Push cpg-analytics-mcp role policies (roles.yaml) down to Snowflake grants and masking policies.

One source of truth, enforced twice: the server's guardrails are the first line of defense,
Snowflake's grants and masks the second. Pure functions that return SQL, so they are testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from cpg_analytics_mcp.config import PACKAGE_DIR

from mcp_warehouse_adapters.snowflake.config import platform_role

LOADER_ROLE = "CPG_LOADER"  # owns the tables and views


def load_roles(path: Path | None = None) -> dict[str, dict]:
    return yaml.safe_load((path or PACKAGE_DIR / "policies" / "roles.yaml").read_text())["roles"]


def grant_statements(roles: dict[str, dict], objects: dict[str, str]) -> list[str]:
    """SELECT grants per role. `objects` maps lowercase name -> "table" | "view"."""
    stmts = []
    for role, spec in roles.items():
        tables = spec.get("tables", "*")
        allowed = sorted(objects) if tables == "*" else sorted(t.lower() for t in tables if t.lower() in objects)
        stmts += [f"GRANT SELECT ON {objects[name].upper()} {name} TO ROLE {platform_role(role)}"
                  for name in allowed]
    return stmts


@dataclass(frozen=True)
class MaskPlan:
    table: str
    column: str
    data_type: str
    allowed_roles: tuple[str, ...]

    def statements(self) -> list[str]:
        name = f"mask_{self.column}"
        roles = ", ".join(f"'{r}'" for r in self.allowed_roles)
        # INVOKER_ROLE() is the view owner when evaluated inside a view, so approved views (owned by
        # CPG_LOADER) can still compute margin from cost: the governed-metric pattern, enforced by Snowflake.
        return [
            f"CREATE OR REPLACE MASKING POLICY {name} AS (val {self.data_type}) RETURNS {self.data_type} -> "
            f"CASE WHEN CURRENT_ROLE() IN ({roles}) OR INVOKER_ROLE() = '{LOADER_ROLE}' THEN val ELSE NULL END",
            f"ALTER TABLE {self.table} MODIFY COLUMN {self.column} SET MASKING POLICY {name}",
        ]


def masking_plans(roles: dict[str, dict], base_columns: dict[str, dict[str, str]]) -> list[MaskPlan]:
    """A mask for every blocked column that exists in a base table. `base_columns`: {table: {column: type}}."""
    blocked = {c.lower() for spec in roles.values() for c in spec.get("blocked_columns", [])}
    plans = []
    for table, cols in sorted(base_columns.items()):
        for col, dtype in cols.items():
            if col in blocked:
                allowed = {platform_role(r) for r, s in roles.items()
                           if col not in {c.lower() for c in s.get("blocked_columns", [])}}
                plans.append(MaskPlan(table, col, dtype, tuple(sorted(allowed | {LOADER_ROLE}))))
    return plans
