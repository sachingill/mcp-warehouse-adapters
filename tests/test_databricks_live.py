"""Live tests against a real Databricks workspace. Skipped unless role credentials exist.

Prerequisites: `uv run mwa-databricks-setup`, then `uv run mwa-databricks-load`.
"""

import pytest
from cpg_analytics_mcp.testing.warehouse_contract import WarehouseContract
from databricks.sql.exc import Error as DatabricksError

from mcp_warehouse_adapters.databricks.config import DatabricksConfig, secret_keys, settings
from mcp_warehouse_adapters.databricks.warehouse import DatabricksWarehouse

pytestmark = pytest.mark.skipif(not settings().get(secret_keys("analyst")[0]), reason="no Databricks credentials")


def connect(role: str, timeout_s: float = 60) -> DatabricksWarehouse:
    return DatabricksWarehouse(DatabricksConfig.for_role(role), timeout_s)


class TestDatabricksContract(WarehouseContract):
    """The same contract DuckDB and Snowflake pass, as the analyst service principal (cost masked)."""

    def make_warehouse(self, timeout_s):
        return connect("analyst", timeout_s)


# --- governance pushed down to Unity Catalog ------------------------------------

def test_analyst_sees_cost_masked():
    rows = connect("analyst").query("SELECT cogs_per_unit FROM dim_product").rows
    assert all(row[0] is None for row in rows)  # masked by Unity Catalog, not by the server


def test_analyst_still_gets_promo_roi_through_the_view():
    res = connect("analyst").query(
        "SELECT SUM(incremental_gross_margin) / SUM(total_trade_spend) FROM v_promo_performance")
    assert res.rows[0][0] is not None


def test_finance_sees_cost():
    rows = connect("finance").query("SELECT cogs_per_unit FROM dim_product").rows
    assert all(row[0] is not None for row in rows)


def test_sales_cannot_read_promo_financials():
    wh = connect("sales")
    assert "v_promo_performance" not in wh.catalog()
    with pytest.raises(DatabricksError):
        wh.query("SELECT * FROM v_promo_performance")


def test_principal_cannot_write():
    wh = connect("analyst")
    with pytest.raises(DatabricksError):
        with wh._con.cursor() as cur:
            cur.execute("CREATE TABLE probe AS SELECT 1 AS x")


@pytest.mark.parametrize("role", ["analyst", "sales", "finance"])
def test_principals_cannot_read_the_raw_layer(role):
    with pytest.raises(DatabricksError):
        connect(role).query("SELECT cogs_per_unit FROM cpg_mcp.raw.dim_product")
