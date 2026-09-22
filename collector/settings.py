"""Shared host settings. Set these through systemd EnvironmentFile or the shell."""

import os
from pathlib import Path

ROOT = Path(os.environ.get("STATE_ROOT", "/var/lib/sub2api-state-reuse"))
STORE = Path(
    os.environ.get(
        "STATE_TICKET_STORE", "/srv/sub2api/data/fn-state-reuse/tickets.json"
    )
)
BASE_URL = os.environ.get("SUB2API_BASE_URL", "http://127.0.0.1:8081/api/v1").rstrip(
    "/"
)
ADMIN_ENV = Path(
    os.environ.get("SUB2API_ENV_FILE", "/etc/sub2api-state-reuse/admin.env")
)
PG_CONTAINER = os.environ.get("SUB2API_PG_CONTAINER", "sub2api-postgres")
PLUGIN_ID = int(os.environ.get("STATE_PLUGIN_ID", "2"))
READY_GROUP = os.environ.get("STATE_READY_GROUP", "不降智分组")
FALLBACK_GROUP = os.environ.get("STATE_FALLBACK_GROUP", "降智分组")
ROUTES_FILE = Path(
    os.environ.get("STATE_ROUTES_FILE", "/etc/sub2api-state-reuse/routes.json")
)
DYNAMIC_PROXY_GENERATORS_FILE = Path(
    os.environ.get(
        "STATE_DYNAMIC_PROXY_GENERATORS_FILE",
        "/etc/sub2api-state-reuse/dynamic-proxy-generators.json",
    )
)
PLUGIN_UID = int(os.environ.get("STATE_PLUGIN_UID", "1000"))
PLUGIN_GID = int(os.environ.get("STATE_PLUGIN_GID", "1000"))
MODEL = "gpt-6-astra"
MODELS = (MODEL, "gpt-5.6-sol")
# Astra remains the account scheduling requirement; Sol is independently probed.
REQUIRED_MODELS = tuple(filter(None, os.environ.get("STATE_REQUIRED_MODELS", MODEL).split(",")))
if not REQUIRED_MODELS or not set(REQUIRED_MODELS).issubset(MODELS):
    raise ValueError("STATE_REQUIRED_MODELS must contain supported probe models")
ACCOUNT_SCOPE = os.environ.get("STATE_ACCOUNT_SCOPE", "groups")
AUTO_GROUP = os.environ.get("STATE_AUTO_GROUP", "true").lower() == "true"
PROXY_SOURCE = os.environ.get("STATE_PROXY_SOURCE", "routes")
TICKET_SCHEDULING = os.environ.get("STATE_TICKET_SCHEDULING", "false").lower() == "true"
SCHEDULING_MARGIN = 30
AUTO_ENROLL = os.environ.get("STATE_AUTO_ENROLL", "false").lower() == "true"
# Upstream honors a harvested STATE bundle (ticket plus the cookies issued
# alongside it) for roughly TICKET_TTL_SECONDS; older bundles stop working.
TICKET_TTL_SECONDS = 240
# Refresh a still-valid bundle once it is older than this, so capture plus
# carry-ticket verification finish before upstream expiry.
TICKET_REFRESH_AFTER_SECONDS = 150
TICKET_REFRESH_INTERVAL_SECONDS = 30


def sql_literal(value):
    return "'" + value.replace("'", "''") + "'"
