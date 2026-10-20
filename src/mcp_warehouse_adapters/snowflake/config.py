"""Snowflake connection settings, read from environment variables. Secrets never live in code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization

REQUIRED = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY_PATH")


def platform_role(mcp_role: str) -> str:
    """Server role -> Snowflake role, so the platform enforces the same policy (analyst -> CPG_ANALYST)."""
    return f"CPG_{mcp_role.upper()}"


@dataclass(frozen=True)
class SnowflakeConfig:
    account: str
    user: str
    private_key_path: Path
    private_key_passphrase: str | None
    role: str
    warehouse: str
    database: str
    schema: str

    @classmethod
    def from_env(cls, mcp_role: str, role_override: str | None = None) -> SnowflakeConfig:
        env = os.environ.get
        missing = [k for k in REQUIRED if not env(k)]
        if missing:
            raise ValueError(f"Snowflake adapter needs environment variables {missing}. See the README.")
        return cls(
            account=env("SNOWFLAKE_ACCOUNT"),
            user=env("SNOWFLAKE_USER"),
            private_key_path=Path(env("SNOWFLAKE_PRIVATE_KEY_PATH")).expanduser(),
            private_key_passphrase=env("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
            role=role_override or env("SNOWFLAKE_ROLE") or platform_role(mcp_role),
            warehouse=env("SNOWFLAKE_WAREHOUSE", "CPG_WH"),
            database=env("SNOWFLAKE_DATABASE", "CPG_MCP"),
            schema=env("SNOWFLAKE_SCHEMA", "MAIN"),
        )

    def private_key_der(self) -> bytes:
        """Decrypt the PKCS#8 key in memory and return it as DER bytes for the connector."""
        password = self.private_key_passphrase.encode() if self.private_key_passphrase else None
        key = serialization.load_pem_private_key(self.private_key_path.read_bytes(), password=password)
        return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())

    def connect_args(self) -> dict:
        return {
            "account": self.account, "user": self.user, "private_key": self.private_key_der(),
            "role": self.role, "warehouse": self.warehouse, "database": self.database, "schema": self.schema,
            "application": "cpg-analytics-mcp",
        }
