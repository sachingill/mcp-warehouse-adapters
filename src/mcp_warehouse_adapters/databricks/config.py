"""Databricks settings. Non-secret values come from the environment; per-role OAuth secrets come from a
private env file written by `mwa-databricks-setup` (default ~/.databricks/cpg_mcp.env, mode 600).

Databricks has no USE ROLE: permissions follow the identity. So each server role logs in as its own
service principal (analyst -> cpg-mcp-analyst, a member of group cpg_analyst).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(os.environ.get("DATABRICKS_CPG_ENV_FILE", "~/.databricks/cpg_mcp.env")).expanduser()


def principal_name(mcp_role: str) -> str:
    return f"cpg-mcp-{mcp_role}"


def group_name(mcp_role: str) -> str:
    return f"cpg_{mcp_role}"


def secret_keys(mcp_role: str) -> tuple[str, str]:
    prefix = f"DATABRICKS_CPG_{mcp_role.upper()}"
    return f"{prefix}_CLIENT_ID", f"{prefix}_CLIENT_SECRET"


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments ignored. Missing file -> {}."""
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines()
             if "=" in line and not line.lstrip().startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def settings(env_file: Path = ENV_FILE) -> dict[str, str]:
    """Env file values, overridden by the process environment."""
    return {**read_env_file(env_file), **os.environ}


@dataclass(frozen=True)
class DatabricksConfig:
    host: str
    http_path: str
    catalog: str
    schema: str
    client_id: str | None = None
    client_secret: str | None = None
    token: str | None = None  # admin PAT: setup and loading only

    @property
    def hostname(self) -> str:
        return self.host.removeprefix("https://").rstrip("/")

    @classmethod
    def _base(cls, env: dict[str, str]) -> dict:
        missing = [k for k in ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH") if not env.get(k)]
        if missing:
            raise ValueError(f"Databricks adapter needs {missing}. Run `uv run mwa-databricks-setup` (README).")
        return {"host": env["DATABRICKS_HOST"], "http_path": env["DATABRICKS_HTTP_PATH"],
                "catalog": env.get("DATABRICKS_CATALOG", "cpg_mcp"), "schema": env.get("DATABRICKS_SCHEMA", "main")}

    @classmethod
    def for_role(cls, mcp_role: str, env_file: Path = ENV_FILE) -> DatabricksConfig:
        """The service principal for a server role (what the MCP server uses)."""
        env = settings(env_file)
        id_key, secret_key = secret_keys(mcp_role)
        if not (env.get(id_key) and env.get(secret_key)):
            raise ValueError(f"No credentials for role '{mcp_role}' ({id_key}). Run `uv run mwa-databricks-setup`.")
        return cls(**cls._base(env), client_id=env[id_key], client_secret=env[secret_key])

    @classmethod
    def admin(cls, env_file: Path = ENV_FILE) -> DatabricksConfig:
        """The workspace admin's personal access token (setup and loading only)."""
        env = settings(env_file)
        if not env.get("DATABRICKS_TOKEN"):
            raise ValueError("Loading needs DATABRICKS_TOKEN (an admin personal access token).")
        return cls(**cls._base(env), token=env["DATABRICKS_TOKEN"])

    def connect_args(self) -> dict:
        args = {"server_hostname": self.hostname, "http_path": self.http_path,
                "catalog": self.catalog, "schema": self.schema, "user_agent_entry": "cpg-analytics-mcp"}
        if self.token:
            return {**args, "access_token": self.token}
        return {**args, "credentials_provider": self._oauth_provider}

    def _oauth_provider(self):
        """OAuth M2M for the service principal. Built lazily: the SDK contacts the host when it creates the
        config, which must happen when the driver connects, not when settings are read. auth_type is explicit
        so an admin DATABRICKS_TOKEN in the environment is never picked up (or rejected as a second method)."""
        from databricks.sdk.core import Config, oauth_service_principal

        return oauth_service_principal(Config(host=self.host, client_id=self.client_id,
                                              client_secret=self.client_secret, auth_type="oauth-m2m"))
