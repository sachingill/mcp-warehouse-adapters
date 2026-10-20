"""One-time workspace setup: one service principal + group per server role, SQL warehouse access,
and an OAuth secret per principal stored in a private env file.

    uv run mwa-databricks-setup            # create (idempotent)
    uv run mwa-databricks-setup --remove   # delete principals, groups and the env file

Runs as the workspace admin (DATABRICKS_HOST + DATABRICKS_TOKEN). Secrets are written to the env file
(mode 600) and never printed.
"""

from __future__ import annotations

import argparse
import os
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam
from databricks.sdk.service import sql as dbsql

from mcp_warehouse_adapters.databricks.config import (
    ENV_FILE,
    group_name,
    principal_name,
    read_env_file,
    secret_keys,
)
from mcp_warehouse_adapters.policy import load_roles


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _find_principal(w: WorkspaceClient, name: str) -> iam.ServicePrincipal | None:
    return next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None)


def _find_group(w: WorkspaceClient, name: str) -> iam.Group | None:
    return next(iter(w.groups.list(filter=f'displayName eq "{name}"')), None)


def _pick_warehouse(w: WorkspaceClient) -> dbsql.EndpointInfo:
    wanted = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    warehouses = list(w.warehouses.list())
    match = [wh for wh in warehouses if wh.id == wanted] if wanted else warehouses
    if not match:
        sys.exit("No SQL warehouse found (set DATABRICKS_WAREHOUSE_ID).")
    return match[0]


def write_env_file(values: dict[str, str]) -> None:
    ENV_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Written by mwa-databricks-setup. Contains OAuth secrets: never commit or share.\n")
        f.writelines(f"{k}={v}\n" for k, v in sorted(values.items()))
    os.chmod(ENV_FILE, 0o600)


def create(w: WorkspaceClient, roles: list[str]) -> None:
    warehouse = _pick_warehouse(w)
    env = read_env_file()
    env["DATABRICKS_HOST"] = w.config.host
    env["DATABRICKS_HTTP_PATH"] = f"/sql/1.0/warehouses/{warehouse.id}"

    for role in roles:
        sp = _find_principal(w, principal_name(role)) or w.service_principals.create(
            display_name=principal_name(role), entitlements=[iam.ComplexValue(value="databricks-sql-access")])
        group = _find_group(w, group_name(role))
        if group is None:
            w.groups.create(display_name=group_name(role), members=[iam.ComplexValue(value=sp.id)])
        elif sp.id not in {m.value for m in (group.members or [])}:
            w.groups.patch(group.id, schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                           operations=[iam.Patch(op=iam.PatchOp.ADD, path="members", value=[{"value": sp.id}])])
        w.warehouses.update_permissions(warehouse.id, access_control_list=[dbsql.WarehouseAccessControlRequest(
            service_principal_name=sp.application_id, permission_level=dbsql.WarehousePermissionLevel.CAN_USE)])

        id_key, secret_key = secret_keys(role)
        if env.get(id_key) != sp.application_id or not env.get(secret_key):
            secret = w.service_principal_secrets_proxy.create(service_principal_id=sp.id)
            env[id_key], env[secret_key] = sp.application_id, secret.secret
            log(f"created OAuth secret for {principal_name(role)}")
        log(f"ready: {principal_name(role)} in group {group_name(role)}, CAN_USE on '{warehouse.name}'")

    write_env_file(env)
    log(f"wrote {ENV_FILE} (mode 600). Next: uv run mwa-databricks-load")


def remove(w: WorkspaceClient, roles: list[str]) -> None:
    for role in roles:
        if sp := _find_principal(w, principal_name(role)):
            w.service_principals.delete(sp.id)
            log(f"deleted {principal_name(role)}")
        if group := _find_group(w, group_name(role)):
            w.groups.delete(group.id)
            log(f"deleted group {group_name(role)}")
    ENV_FILE.unlink(missing_ok=True)
    log(f"removed {ENV_FILE}. The cpg_mcp catalog is kept; drop it in the SQL editor if you want.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remove", action="store_true", help="delete principals, groups and the env file")
    args = parser.parse_args()
    if not (os.environ.get("DATABRICKS_HOST") and os.environ.get("DATABRICKS_TOKEN")):
        sys.exit("Set DATABRICKS_HOST and DATABRICKS_TOKEN (admin personal access token).")
    w = WorkspaceClient()
    roles = list(load_roles())
    remove(w, roles) if args.remove else create(w, roles)


if __name__ == "__main__":
    main()
