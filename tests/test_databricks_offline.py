"""Offline tests: no Databricks workspace needed. Run in CI."""

import sqlglot
from cpg_analytics_mcp.data.generate import VIEWS

from mcp_warehouse_adapters.databricks.config import DatabricksConfig, read_env_file, secret_keys
from mcp_warehouse_adapters.databricks.governance import analysis_view, dynamic_view_statements, grant_statements
from mcp_warehouse_adapters.databricks.warehouse import is_timeout, to_named
from mcp_warehouse_adapters.policy import load_roles

OBJECTS = {
    "dim_week": "table", "dim_product": "table", "dim_retailer": "table", "fact_promotions": "table",
    "fact_sales": "table", "fact_shipments": "table", "fact_targets": "table",
    "v_sales_enriched": "view", "v_promo_performance": "view", "v_ship_attainment": "view",
}


def test_placeholders_become_named_but_literals_are_untouched():
    sql, params = to_named("SELECT '?' AS q FROM v WHERE brand IN (?, ?) AND d >= CAST(? AS DATE)", ["a", "b", "c"])
    assert sql == "SELECT '?' AS q FROM v WHERE brand IN (:p0, :p1) AND d >= CAST(:p2 AS DATE)"
    assert params == {"p0": "a", "p1": "b", "p2": "c"}
    assert to_named("SELECT 1", None) == ("SELECT 1", {})


def test_timeout_errors_are_recognised():
    assert is_timeout(Exception("Query has been timed out due to inactivity"))
    assert is_timeout(Exception("[STATEMENT_TIMEOUT] statement exceeded 1 seconds"))
    assert not is_timeout(Exception("[TABLE_OR_VIEW_NOT_FOUND] v_x"))


def test_env_file_and_role_credentials(tmp_path, monkeypatch):
    for k in ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", *secret_keys("sales")):
        monkeypatch.delenv(k, raising=False)
    env_file = tmp_path / "cpg_mcp.env"
    env_file.write_text("# comment\nDATABRICKS_HOST=https://dbc-x.cloud.databricks.com\n"
                        "DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc\n"
                        "DATABRICKS_CPG_SALES_CLIENT_ID=app-123\nDATABRICKS_CPG_SALES_CLIENT_SECRET=s3cr3t\n")
    assert read_env_file(env_file)["DATABRICKS_CPG_SALES_CLIENT_ID"] == "app-123"
    cfg = DatabricksConfig.for_role("sales", env_file)
    assert cfg.hostname == "dbc-x.cloud.databricks.com" and cfg.client_id == "app-123" and cfg.token is None
    assert "credentials_provider" in cfg.connect_args() and "access_token" not in cfg.connect_args()


def test_grants_mirror_roles_yaml():
    grantees = {"analyst": "app-a", "sales": "app-s", "finance": "app-f"}
    grants = grant_statements(load_roles(), OBJECTS, "cpg_mcp", "main", grantees)
    sales = [g for g in grants if g.endswith("TO `app-s`")]
    assert "GRANT USE CATALOG ON CATALOG cpg_mcp TO `app-s`" in sales
    assert "GRANT SELECT ON TABLE cpg_mcp.main.v_sales_enriched TO `app-s`" in sales
    assert sum(g.startswith("GRANT SELECT") for g in sales) == 6
    assert not any("v_promo_performance" in g for g in sales)


def test_dynamic_view_hides_cost_except_for_finance_and_owners():
    cols = {"dim_product": ["sku_id", "brand", "cogs_per_unit"], "dim_retailer": ["retailer_id", "channel"]}
    types = {t: {c: "STRING" for c in cs} for t, cs in cols.items()} | {"dim_product": {
        "sku_id": "STRING", "brand": "STRING", "cogs_per_unit": "DOUBLE"}}
    product, retailer = dynamic_view_statements(load_roles(), cols, types, "cpg_mcp", "main")
    assert product == ("CREATE OR REPLACE VIEW cpg_mcp.main.dim_product AS SELECT sku_id, brand, "
                       "CASE WHEN is_member('cpg_finance') OR is_member('admins') THEN cogs_per_unit END "
                       "AS cogs_per_unit FROM cpg_mcp.raw.dim_product")
    assert retailer.endswith("SELECT retailer_id, channel FROM cpg_mcp.raw.dim_retailer")


def test_analysis_views_live_in_main_and_read_raw():
    create = sqlglot.transpile(VIEWS, read="duckdb", write="databricks")[1]  # v_promo_performance
    out = analysis_view(create, "cpg_mcp", "main")
    assert out.startswith("CREATE OR REPLACE VIEW cpg_mcp.main.v_promo_performance")
    assert "cpg_mcp.raw.dim_product" in out and "cpg_mcp.main.dim_product" not in out
    assert "cpg_mcp.raw.calc" not in out  # CTE names are not tables


def test_views_translate_to_databricks():
    statements = sqlglot.transpile(VIEWS, read="duckdb", write="databricks")
    assert len(statements) == 3
    for s in statements:
        sqlglot.parse_one(s, read="databricks")


def test_robot_login_never_falls_back_to_admin_token(monkeypatch):
    # with DATABRICKS_TOKEN in the environment the SDK would otherwise see two auth methods
    import databricks.sdk.core as core

    seen = {}
    monkeypatch.setattr(core, "Config", lambda **kw: seen.update(kw) or kw)
    monkeypatch.setattr(core, "oauth_service_principal", lambda cfg: "provider")
    cfg = DatabricksConfig(host="https://h", http_path="/p", catalog="c", schema="s", client_id="id", client_secret="x")
    assert cfg.connect_args()["credentials_provider"]() == "provider"
    assert seen["auth_type"] == "oauth-m2m"
