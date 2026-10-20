"""One-time account setup: run setup.sql as ACCOUNTADMIN, with the service user's public key read from disk.

    uv run mwa-snowflake-setup

The key never lives in the repo: setup.sql keeps a `<PUBLIC_KEY>` placeholder, filled in memory from
SNOWFLAKE_PUBLIC_KEY_PATH (default ~/.snowflake/cpg_mcp_key.pub). The admin logs in through the browser
(SSO / password + MFA), so no admin credentials are stored either.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import snowflake.connector

SETUP_SQL = Path(__file__).with_name("setup.sql")
PLACEHOLDER = "<PUBLIC_KEY>"


def public_key_body(path: Path) -> str:
    """The base64 body of a PEM public key, without BEGIN/END lines, as Snowflake expects."""
    text = path.expanduser().read_text()
    body = "".join(line.strip() for line in text.strip().splitlines() if "PUBLIC KEY" not in line)
    if not body or "PRIVATE" in text:
        raise ValueError(f"{path} does not look like a PEM public key.")
    return body


def render(sql: str, public_key: str) -> str:
    if PLACEHOLDER not in sql:
        raise ValueError(f"setup.sql must contain the {PLACEHOLDER} placeholder, never a real key.")
    return sql.replace(PLACEHOLDER, public_key)


def main() -> None:
    env = os.environ.get
    missing = [k for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_ADMIN_USER") if not env(k)]
    if missing:
        sys.exit(f"Set {missing} (SNOWFLAKE_ADMIN_USER = the user you log in to Snowsight with).")
    key_path = Path(env("SNOWFLAKE_PUBLIC_KEY_PATH", "~/.snowflake/cpg_mcp_key.pub")).expanduser()
    sql = render(SETUP_SQL.read_text(), public_key_body(key_path))

    con = snowflake.connector.connect(account=env("SNOWFLAKE_ACCOUNT"), user=env("SNOWFLAKE_ADMIN_USER"),
                                      authenticator="externalbrowser", role="ACCOUNTADMIN")
    try:
        for cur in con.execute_string(sql, remove_comments=True):
            print(f"ok  {cur.query.splitlines()[0][:90]}", file=sys.stderr)
    finally:
        con.close()
    print(f"setup complete; service user key taken from {key_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
