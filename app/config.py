"""Configuration — every value comes from the environment, nothing is hardcoded.

Secrets are read at start time from the environment (a mode-600 env file, a
Docker secret, whatever you prefer) and never fetched at runtime, so a secrets
store being unreachable can never take the dashboard offline.

An optional YAML file can supply any of these too — see `settings_file`. An
explicit environment variable always wins over the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import settings_file

# Must run before the dataclass below is defined: its field defaults are
# evaluated at class-creation time, so anything applied after that is ignored.
settings_file.load()


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    # --- Home Assistant ---
    ha_url: str = os.environ.get("HA_URL", "http://192.168.1.20:8123")
    ha_token: str = os.environ.get("HA_TOKEN", "")

    # --- Proxmox ---
    pve_host: str = os.environ.get("PVE_HOST", "https://192.168.1.24:8006")
    pve_token_id: str = os.environ.get("PVE_TOKEN_ID", "")
    pve_token_secret: str = os.environ.get("PVE_TOKEN_SECRET", "")
    pve_nodes: tuple[str, ...] = tuple(
        n for n in os.environ.get("PVE_NODES", "pve,pve3,pve4").split(",") if n
    )
    pve_verify_tls: bool = _b("PVE_VERIFY_TLS", False)

    # --- UniFi (optional; panel degrades cleanly without it) ---
    unifi_host: str = os.environ.get("UNIFI_HOST", "https://192.168.1.1")
    unifi_api_key: str = os.environ.get("UNIFI_API_KEY", "")
    unifi_site: str = os.environ.get("UNIFI_SITE", "default")
    # Which networks carry visitors, and which are yours. Facts about a particular
    # house rather than about the code, so they are configuration: anything that
    # turns up on a guest network is worth knowing about.
    unifi_guest_networks: tuple[str, ...] = tuple(
        n.strip() for n in os.environ.get("UNIFI_GUEST_NETWORKS", "Guest").split(",") if n.strip()
    )
    unifi_trusted_networks: tuple[str, ...] = tuple(
        n.strip() for n in os.environ.get(
            "UNIFI_TRUSTED_NETWORKS", "Home").split(",") if n.strip()
    )
    # A client first seen within this window is "new". Long enough to survive a weekend
    # away, short enough that the warning clears itself without anyone dismissing it.
    unifi_new_device_days: int = _i("UNIFI_NEW_DEVICE_DAYS", 7)

    # --- GameVault (a physical game collection; optional) ---
    # Point straight at the service rather than at a hostname behind SSO: a
    # dashboard poll would collect a login page instead of JSON. Blank disables.
    gamevault_url: str = os.environ.get("GAMEVAULT_URL", "")

    # --- Bookmarks: Nextcloud Bookmarks (also Floccus's sync backend) ---
    # Auth is a Nextcloud *app password*, never the account password: it is
    # revocable on its own and keeps working if SSO is put in front later.
    nextcloud_url: str = os.environ.get("NEXTCLOUD_URL", "")
    nextcloud_user: str = os.environ.get("NEXTCLOUD_USER", "")
    nextcloud_password: str = os.environ.get("NEXTCLOUD_PASSWORD", "")
    nextcloud_inbox: str = os.environ.get("NEXTCLOUD_INBOX", "Inbox")

    # Some sensors are event-driven and legitimately silent for days — a dry
    # SimpliSafe leak sensor never publishes. Judging those by their own
    # last_updated cries wolf. Instead we watch a HEARTBEAT entity from the same
    # integration: one that does update on a poll. If the heartbeat is fresh the
    # integration is alive, so silence from the sensor means "dry", not "gone".
    leak_heartbeat: str = os.environ.get(
        "LEAK_HEARTBEAT", "alarm_control_panel.alarm_control_panel"
    )
    leak_heartbeat_max_age: int = _i("LEAK_HEARTBEAT_MAX_AGE", 3600)

    # --- Push ingest (patch debt is pushed to us; see docs/PLAN.md) ---
    ingest_token: str = os.environ.get("INGEST_TOKEN", "")

    # Devices to never report as "not responding" (comma-separated, exact HA device
    # names). Read by panels.py directly from the environment.
    # --- Gaming progress (optional) ---
    # An hourly JSON export of achievements/progress. Blank disables the panel.
    gamelog_url: str = os.environ.get("GAMELOG_URL", "")

    # --- TLS expiry watch ---
    cert_host: str = os.environ.get("CERT_HOST", "")

    # --- runtime ---
    db_path: str = os.environ.get("DB_PATH", "/var/lib/sagamore/sagamore.db")
    poll_seconds: int = _i("POLL_SECONDS", 60)
    slow_poll_seconds: int = _i("SLOW_POLL_SECONDS", 900)
    timezone: str = os.environ.get("TZ", "America/New_York")
    http_timeout: int = _i("HTTP_TIMEOUT", 20)

    # Circuits treated as "always on by design" so the phantom-load view
    # doesn't nag about them. Comma-separated entity_id substrings.
    baseline_circuits: tuple[str, ...] = tuple(
        s.strip() for s in os.environ.get(
            "BASELINE_CIRCUITS", "networkrack,sump_pump,waterheater,basementfridge,wine_fridge"
        ).split(",") if s.strip()
    )

    missing: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Return a list of human-readable problems. Never raises.

        A missing optional integration degrades that panel to 'not configured'
        rather than taking the whole dashboard down — the point of this thing is
        to be honest about what it can and cannot see.
        """
        problems: list[str] = []
        if not self.ha_token:
            problems.append("HA_TOKEN is not set — house panels will be empty")
        if not (self.pve_token_id and self.pve_token_secret):
            problems.append("PVE_TOKEN_ID/SECRET not set — homelab panel will be empty")
        return problems


CONFIG = Config()
