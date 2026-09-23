"""Optional YAML config file, layered *under* the environment.

Sagamore has always been configured by environment variables, which suits a
systemd unit reading a mode-600 env file. It suits Docker less well: editing a
setting means editing compose YAML and recreating the container.

So a config file is supported too, in the spirit of Homepage's `services.yaml` —
mount one file, edit it, restart. Nothing else changes: this module simply
translates the file into environment variables before `config.py` reads them.

**Precedence, highest first:**

1. An explicit environment variable — so `docker run -e HA_TOKEN=…` still wins,
   and secrets can stay out of the file entirely.
2. The config file.
3. The built-in default.

That ordering is deliberate. Secrets belong in the environment (or a secrets
store that renders one); the file is for the settings you actually want to sit
down and edit.

**Where it looks**, first match wins:

- `$CONFIG_FILE`
- `./config/sagamore.yaml`
- `/config/sagamore.yaml`      (the Docker convention)
- `/etc/sagamore/sagamore.yaml`

If no file exists, or PyYAML isn't installed, this is a no-op and Sagamore
behaves exactly as it always has.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # PyYAML is optional: without it, the environment is the only source.
    import yaml
except ImportError:  # pragma: no cover - exercised only on a minimal install
    yaml = None  # type: ignore[assignment]


# dotted path in the YAML  ->  environment variable name
_SCALARS: dict[str, str] = {
    "home_assistant.url": "HA_URL",
    "home_assistant.token": "HA_TOKEN",
    "proxmox.host": "PVE_HOST",
    "proxmox.token_id": "PVE_TOKEN_ID",
    "proxmox.token_secret": "PVE_TOKEN_SECRET",
    "proxmox.verify_tls": "PVE_VERIFY_TLS",
    "unifi.host": "UNIFI_HOST",
    "unifi.api_key": "UNIFI_API_KEY",
    "unifi.site": "UNIFI_SITE",
    "unifi.new_device_days": "UNIFI_NEW_DEVICE_DAYS",
    "bookmarks.url": "NEXTCLOUD_URL",
    "bookmarks.user": "NEXTCLOUD_USER",
    "bookmarks.password": "NEXTCLOUD_PASSWORD",
    "bookmarks.inbox": "NEXTCLOUD_INBOX",
    "gamevault.url": "GAMEVAULT_URL",
    "gamelog.url": "GAMELOG_URL",
    "leak.heartbeat": "LEAK_HEARTBEAT",
    "leak.heartbeat_max_age": "LEAK_HEARTBEAT_MAX_AGE",
    "tls.cert_host": "CERT_HOST",
    "ingest.token": "INGEST_TOKEN",
    "runtime.db_path": "DB_PATH",
    "runtime.timezone": "TZ",
    "runtime.poll_seconds": "POLL_SECONDS",
    "runtime.slow_poll_seconds": "SLOW_POLL_SECONDS",
    "runtime.http_timeout": "HTTP_TIMEOUT",
}

# dotted path -> env var, for YAML lists that become comma-separated strings
_LISTS: dict[str, str] = {
    "proxmox.nodes": "PVE_NODES",
    "unifi.guest_networks": "UNIFI_GUEST_NETWORKS",
    "unifi.trusted_networks": "UNIFI_TRUSTED_NETWORKS",
    "ignore_devices": "HA_IGNORE_DEVICES",
    "baseline_circuits": "BASELINE_CIRCUITS",
}

_CANDIDATES = (
    os.environ.get("CONFIG_FILE"),
    "./config/sagamore.yaml",
    "/config/sagamore.yaml",
    "/etc/sagamore/sagamore.yaml",
)


def _dig(data: dict[str, Any], dotted: str) -> Any:
    """Walk a dotted path, returning None rather than raising on any miss."""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _as_env(value: Any) -> str:
    """Render a scalar the way the environment would carry it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def find_config_file() -> Path | None:
    for candidate in _CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def load(path: Path | None = None) -> dict[str, str]:
    """Apply a YAML config file to os.environ. Returns what it actually set.

    Never raises: a malformed or unreadable file leaves the environment alone,
    because a dashboard that refuses to start is worse than one running on
    defaults and saying so.
    """
    path = path or find_config_file()
    if path is None or yaml is None:
        return {}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    applied: dict[str, str] = {}

    def put(env_name: str, value: str) -> None:
        # An explicit environment variable always wins.
        if os.environ.get(env_name):
            return
        os.environ[env_name] = value
        applied[env_name] = value

    for dotted, env_name in _SCALARS.items():
        value = _dig(data, dotted)
        if value is not None:
            put(env_name, _as_env(value))

    for dotted, env_name in _LISTS.items():
        value = _dig(data, dotted)
        if isinstance(value, (list, tuple)):
            put(env_name, ",".join(str(v).strip() for v in value if str(v).strip()))
        elif isinstance(value, str) and value.strip():
            put(env_name, value.strip())

    # Quick links are a list of mappings in YAML, but the app reads the older
    # "Name|url|icon,Name|url|icon" form. Translate rather than change the app.
    favourites = data.get("favorites") or data.get("favourites")
    if isinstance(favourites, list):
        entries = []
        for item in favourites:
            if not isinstance(item, dict):
                continue
            name, url = item.get("name"), item.get("url")
            icon = item.get("icon", "")
            if name and url:
                entries.append(f"{name}|{url}|{icon}")
        if entries:
            put("FAVORITES", ",".join(entries))

    return applied
