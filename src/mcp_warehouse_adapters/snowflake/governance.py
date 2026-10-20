"""Push cpg-analytics-mcp role policies (roles.yaml) down to Snowflake grants and masking policies.

One source of truth, enforced twice: the server's guardrails are the first line of defense,
Snowflake's grants and masks the second. Pure functions that return SQL, so they are testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp_warehouse_adapters.policy import allowed_objects, load_roles, mask_targets
from mcp_warehouse_adapters.snowflake.config import platform_role

__all__ = ["LOADER_ROLE", "MaskPlan", "grant_statements", "load_roles", "masking_plans"]

LOADER_ROLE = "CPG_LOADER"  # owns the tables and views


def grant_statements(roles: dict[str, dict], objects: dict[str, str]) -> list[str]:
    """SELECT grants per role. `objects` maps lowercase name -> "table" | "view"."""
    return [f"GRANT SELECT ON {objects[name].upper()} {name} TO ROLE {platform_role(role)}"
            for role, spec in roles.items() for name in allowed_objects(spec, objects)]


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
    return [MaskPlan(t.table, t.column, t.data_type,
                     tuple(sorted({platform_role(r) for r in t.allowed_roles} | {LOADER_ROLE})))
            for t in mask_targets(roles, base_columns)]
