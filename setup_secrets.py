"""
One-time setup script: stores the Lakebase connection URL as a Databricks
secret. Run this locally (with the Databricks CLI configured) or from a
notebook - never commit the resulting secret value anywhere.

This is the ONLY secret this project uses - Open-Meteo (the weather API)
needs no key at all. Skip this script entirely if you already have a
"database/lakebase-url" secret from a prior Lakebase homework; both
mcp_server/app.yaml and dashboard/app.yaml already point at that
scope/key by default.

Usage:
    python setup_secrets.py
"""

import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

w = WorkspaceClient()

w.secrets.create_scope(scope="database")
w.secrets.put_secret(
    scope="database",
    key="lakebase-url",
    string_value=getpass.getpass("Paste your Lakebase connection URL: "),
)
w.secrets.put_acl(
    scope="database",
    principal="users",
    permission=workspace.AclPermission.READ,
)

print("Stored database/lakebase-url. Run mcp_server/schema_weather_log.sql "
      "(or just start the app - it self-creates the table) to finish setup.")
