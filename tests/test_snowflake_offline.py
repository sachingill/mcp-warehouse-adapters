"""Offline tests: no Snowflake account needed. Run in CI."""

from decimal import Decimal

import pytest
import sqlglot
from cpg_analytics_mcp.data.generate import VIEWS

from mcp_warehouse_adapters.snowflake.config import SnowflakeConfig, platform_role
from mcp_warehouse_adapters.snowflake.governance import LOADER_ROLE, grant_statements, load_roles, masking_plans
from mcp_warehouse_adapters.snowflake.warehouse import plain

OBJECTS = {
    "dim_week": "table", "dim_product": "table", "dim_retailer": "table", "fact_promotions": "table",
    "fact_sales": "table", "fact_shipments": "table", "fact_targets": "table",
    "v_sales_enriched": "view", "v_promo_performance": "view", "v_ship_attainment": "view",
}


def test_plain_converts_decimals():
    assert plain(Decimal("42")) == 42 and isinstance(plain(Decimal("42")), int)
    assert plain(Decimal("0.89")) == pytest.approx(0.89) and isinstance(plain(Decimal("0.89")), float)
    assert plain("x") == "x" and plain(None) is None


def test_server_role_maps_to_platform_role():
    assert platform_role("analyst") == "CPG_ANALYST"


def test_config_requires_env(monkeypatch):
    for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError, match="SNOWFLAKE_ACCOUNT"):
        SnowflakeConfig.from_env("analyst")


def test_config_role_follows_server_role_unless_overridden(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ORG-ACCT")
    monkeypatch.setenv("SNOWFLAKE_USER", "CPG_MCP_SVC")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "~/.snowflake/key.p8")
    monkeypatch.delenv("SNOWFLAKE_ROLE", raising=False)
    assert SnowflakeConfig.from_env("sales").role == "CPG_SALES"
    assert SnowflakeConfig.from_env("sales", role_override=LOADER_ROLE).role == LOADER_ROLE


def test_grants_mirror_roles_yaml():
    grants = grant_statements(load_roles(), OBJECTS)
    sales = [g for g in grants if g.endswith("TO ROLE CPG_SALES")]
    assert len(sales) == 6  # the six objects listed for sales in roles.yaml
    assert "GRANT SELECT ON VIEW v_sales_enriched TO ROLE CPG_SALES" in sales
    assert not any("v_promo_performance" in g for g in sales)
    assert sum(g.endswith("TO ROLE CPG_ANALYST") for g in grants) == len(OBJECTS)  # tables: "*"


def test_cost_is_masked_for_everyone_but_finance():
    plans = masking_plans(load_roles(), {"dim_product": {"sku_id": "TEXT", "cogs_per_unit": "FLOAT"}})
    assert len(plans) == 1
    plan = plans[0]
    assert plan.column == "cogs_per_unit" and plan.allowed_roles == ("CPG_FINANCE", LOADER_ROLE)
    create, attach = plan.statements()
    assert "INVOKER_ROLE() = 'CPG_LOADER'" in create  # views owned by the loader still compute margin
    assert attach == "ALTER TABLE dim_product MODIFY COLUMN cogs_per_unit SET MASKING POLICY mask_cogs_per_unit"


def test_views_translate_to_snowflake():
    statements = sqlglot.transpile(VIEWS, read="duckdb", write="snowflake")
    assert len(statements) == 3
    for s in statements:
        sqlglot.parse_one(s, read="snowflake")  # round-trips as valid Snowflake SQL
        assert "IGNORE NULLS" not in s


def test_table_ddl_translates_duckdb_types():
    from mcp_warehouse_adapters.snowflake.loader import export_sql, snowflake_ddl

    cols = [("week_ending", "TIMESTAMP_S"), ("units", "BIGINT"), ("price", "DOUBLE"), ("promo_id", "VARCHAR")]
    ddl = snowflake_ddl("fact_sales", cols)
    assert ddl.startswith("CREATE OR REPLACE TABLE fact_sales")
    assert "week_ending TIMESTAMPNTZ" in ddl and "TIMESTAMP_S" not in ddl
    sqlglot.parse_one(ddl, read="snowflake")
    assert "CAST(week_ending AS TIMESTAMP) AS week_ending" in export_sql("fact_sales", cols, "/tmp/x.parquet")
