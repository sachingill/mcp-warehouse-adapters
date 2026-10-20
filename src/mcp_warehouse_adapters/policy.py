"""Read cpg-analytics-mcp roles.yaml once. Platform modules turn the result into their own GRANT / mask SQL.

Shared because "which role reads which objects" and "which columns are hidden from whom" is the same
knowledge on every platform; only the SQL that enforces it differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from cpg_analytics_mcp.config import PACKAGE_DIR


def load_roles(path: Path | None = None) -> dict[str, dict]:
    return yaml.safe_load((path or PACKAGE_DIR / "policies" / "roles.yaml").read_text())["roles"]


def allowed_objects(spec: dict, objects: dict[str, str]) -> list[str]:
    """Objects (lowercase names) a role may SELECT from. `objects` maps name -> "table" | "view"."""
    tables = spec.get("tables", "*")
    return sorted(objects) if tables == "*" else sorted(t.lower() for t in tables if t.lower() in objects)


@dataclass(frozen=True)
class MaskTarget:
    table: str
    column: str
    data_type: str
    allowed_roles: tuple[str, ...]  # server role names that may see the real value


def mask_targets(roles: dict[str, dict], base_columns: dict[str, dict[str, str]]) -> list[MaskTarget]:
    """Every blocked column that exists in a base table. `base_columns`: {table: {column: type}}."""
    blocked = {c.lower() for spec in roles.values() for c in spec.get("blocked_columns", [])}
    targets = []
    for table, cols in sorted(base_columns.items()):
        for col, dtype in cols.items():
            if col in blocked:
                allowed = tuple(sorted(r for r, s in roles.items()
                                       if col not in {c.lower() for c in s.get("blocked_columns", [])}))
                targets.append(MaskTarget(table, col, dtype, allowed))
    return targets


def plain(value: Any) -> Any:
    """Driver values -> plain Python, as the Warehouse contract requires (no Decimal)."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value.as_tuple().exponent >= 0 else float(value)
    return value


def export_sql(table: str, columns: list[tuple[str, str]], path: Path) -> str:
    """DuckDB COPY to Parquet, with timestamps normalised to microsecond TIMESTAMP (portable everywhere)."""
    cols = ", ".join(f"CAST({n} AS TIMESTAMP) AS {n}" if ty.startswith("TIMESTAMP") else n for n, ty in columns)
    return f"COPY (SELECT {cols} FROM {table}) TO '{path}' (FORMAT PARQUET)"
