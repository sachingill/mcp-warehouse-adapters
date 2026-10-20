# mcp-warehouse-adapters

[![CI](https://github.com/sachingill/mcp-warehouse-adapters/actions/workflows/ci.yml/badge.svg)](https://github.com/sachingill/mcp-warehouse-adapters/actions/workflows/ci.yml)

Run [cpg-analytics-mcp](https://github.com/sachingill/cpg-analytics-mcp), a governed MCP server for CPG
commercial analytics, on **Snowflake** (Databricks next) instead of DuckDB.

## Why this exists

Enterprises won't copy their data into a local database. An AI analytics server has to run on *their* platform
and respect *their* permissions. So governance is pushed **down** to the platform:

| Concern | Server (first line) | Snowflake (second line) |
|---|---|---|
| Who reads which tables | `roles.yaml` table allowlists | `SELECT` grants generated from the same `roles.yaml` |
| Unit cost (`cogs_per_unit`) | Blocked column in the SQL guardrails | **Masking policy**: `NULL` for analyst and sales roles |
| Promo ROI still available to analysts | Governed metric | Views owned by the loader role compute with cost (`INVOKER_ROLE()`) |
| Writes | Guardrails allow one `SELECT` only | Read-only roles have no write privileges |
| Accountability | JSONL audit log | `QUERY_TAG` on every statement (`mcp_role` included) |
| Runaway queries | Row cap | `STATEMENT_TIMEOUT_IN_SECONDS` + resource monitor credit cap |

If a guardrail had a bug, Snowflake would still refuse.

## How it plugs in

The server talks to any warehouse through a two-method interface (`catalog()`, `query()`). This package registers
`snowflake` under the `cpg_analytics_mcp.warehouses` entry point, so `CPG_MCP_WAREHOUSE=snowflake` is all the
server needs. Governed SQL is written once in DuckDB dialect and translated with sqlglot. Every adapter must pass
the same [contract tests](https://github.com/sachingill/cpg-analytics-mcp/blob/main/src/cpg_analytics_mcp/testing/warehouse_contract.py)
as DuckDB. See the [design note](https://github.com/sachingill/cpg-analytics-mcp/blob/main/docs/warehouse-adapters.md).

## Snowflake quickstart

Use a personal trial account and the synthetic data only.

1. **Key pair** for the service user (your passphrase stays with you):
   ```bash
   mkdir -p ~/.snowflake && chmod 700 ~/.snowflake
   openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes-256-cbc -inform PEM -out ~/.snowflake/cpg_mcp_key.p8
   openssl rsa -in ~/.snowflake/cpg_mcp_key.p8 -pubout -out ~/.snowflake/cpg_mcp_key.pub
   chmod 600 ~/.snowflake/cpg_mcp_key.p8
   ```
2. **Environment** (in your shell profile, never in the repo):
   ```bash
   export SNOWFLAKE_ACCOUNT=<orgname-accountname>
   export SNOWFLAKE_ADMIN_USER=<your Snowsight login>        # setup only
   export SNOWFLAKE_PUBLIC_KEY_PATH=~/.snowflake/cpg_mcp_key.pub
   export SNOWFLAKE_USER=CPG_MCP_SVC
   export SNOWFLAKE_PRIVATE_KEY_PATH=~/.snowflake/cpg_mcp_key.p8
   export SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=...   # or leave unset and use an unencrypted key
   ```
3. **Account setup**, once: runs [`setup.sql`](src/mcp_warehouse_adapters/snowflake/setup.sql) as `ACCOUNTADMIN`
   (browser login), filling the service user's public key from `SNOWFLAKE_PUBLIC_KEY_PATH` in memory. It creates
   an XS warehouse with a credit cap, the `CPG_MCP.MAIN` schema, one loader role plus one read-only role per
   server role, and a key-pair-only service user.
   ```bash
   uv sync
   uv run mwa-snowflake-setup
   ```
4. **Load, test, run:**
   ```bash
   uv run mwa-snowflake-load        # synthetic data -> Parquet -> COPY INTO; views; grants; masks
   uv run pytest -q                 # offline tests + live contract and governance tests
   CPG_MCP_WAREHOUSE=snowflake CPG_MCP_ROLE=analyst uv run cpg-analytics-mcp
   ```

The server role selects the Snowflake role (`analyst` → `CPG_ANALYST`), so the same question gets the same policy
from both lines of defense.

## Layout

```
src/mcp_warehouse_adapters/snowflake/
  setup.sql       one-time account setup; <PUBLIC_KEY> placeholder only
  setup.py        runs setup.sql as ACCOUNTADMIN, key read from SNOWFLAKE_PUBLIC_KEY_PATH
  config.py       env-based settings, key-pair loading, server role -> platform role
  warehouse.py    SnowflakeWarehouse: qmark binding, query tag, timeout -> QueryTimeout, lowercase, no Decimal
  governance.py   roles.yaml -> GRANT statements and masking policies (pure, tested offline)
  loader.py       bulk load + views (DDL and views translated from DuckDB with sqlglot)
tests/
  test_snowflake_offline.py   no account needed (runs in CI)
  test_snowflake_live.py      contract + platform governance (skipped without credentials)
```

## Roadmap

- [x] Snowflake adapter, loader, governance push-down
- [ ] Databricks adapter: SQL warehouse, OAuth, Unity Catalog grants and column masks, system-table audit
- [ ] Identity propagation: queries run as the end user via OAuth (REST statement APIs)

MIT licensed. All data is synthetic.
