"""Live tests against a real Snowflake account. Skipped unless SNOWFLAKE_ACCOUNT is set.

Prerequisites: `uv run mwa-snowflake-setup` once, then `uv run mwa-snowflake-load`.
"""

import os

import pytest
from cpg_analytics_mcp.testing.warehouse_contract import WarehouseContract
from snowflake.connector.errors import ProgrammingError

from mcp_warehouse_adapters.snowflake.config import SnowflakeConfig
from mcp_warehouse_adapters.snowflake.warehouse import SnowflakeWarehouse

pytestmark = pytest.mark.skipif(not os.environ.get("SNOWFLAKE_ACCOUNT"), reason="no Snowflake credentials")


def connect(role: str, timeout_s: float = 30) -> SnowflakeWarehouse:
    return SnowflakeWarehouse(SnowflakeConfig.from_env(role), timeout_s, query_tag={"app": "mwa-tests"})


class TestSnowflakeContract(WarehouseContract):
    """The same contract DuckDB passes, run as the analyst role (cost masked)."""

    def make_warehouse(self, timeout_s):
        return connect("analyst", timeout_s)


# --- governance pushed down to the platform -------------------------------------

def test_analyst_sees_cost_masked_but_gets_roi():
    wh = connect("analyst")
    cost = wh.query("SELECT cogs_per_unit FROM dim_product")
    assert all(row[0] is None for row in cost.rows)  # masked by Snowflake, not by the server
    roi = wh.query("SELECT SUM(incremental_gross_margin) / SUM(total_trade_spend) FROM v_promo_performance")
    assert roi.rows[0][0] is not None  # governed metric: the loader-owned view can still use cost


def test_finance_sees_cost():
    rows = connect("finance").query("SELECT cogs_per_unit FROM dim_product").rows
    assert all(row[0] is not None for row in rows)


def test_sales_cannot_read_promo_financials():
    wh = connect("sales")
    assert "v_promo_performance" not in wh.catalog()
    with pytest.raises(ProgrammingError):
        wh.query("SELECT * FROM v_promo_performance")


def test_secondary_roles_are_off():
    # otherwise a session silently carries every role granted to the user
    wh = connect("sales")
    assert wh.query("SELECT CURRENT_SECONDARY_ROLES()").rows[0][0] in ('{"roles":"","value":""}', "")


def test_analyst_role_cannot_write():
    wh = connect("analyst")
    with pytest.raises(ProgrammingError):
        wh._con.cursor().execute("CREATE TABLE probe AS SELECT 1 AS x")
