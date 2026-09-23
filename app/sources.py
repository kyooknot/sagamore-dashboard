"""Collectors — everything that reaches out to another system.

Every collector returns plain data and **never raises**: a source being down
degrades one panel to UNKNOWN instead of blanking the dashboard. That's the same
rule the Red Sox poller and the sprinkler pipeline follow — one failing source
must not take the others with it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import ssl
import time
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("sagamore.sources")


class SourceError(Exception):
    pass


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------
class HomeAssistant:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @property
    def configured(self) -> bool:
        return bool(self.cfg.ha_token)

    async def states(self) -> dict[str, dict]:
        """Every entity, keyed by entity_id. One call feeds every house panel."""
        if not self.configured:
            raise SourceError("HA_TOKEN not set")
        url = f"{self.cfg.ha_url.rstrip('/')}/api/states"
        headers = {"Authorization": f"Bearer {self.cfg.ha_token}"}
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        return {e["entity_id"]: e for e in data}


    async def unavailable_devices(self) -> dict[str, tuple[int, int]]:
        """{device name: (unavailable entities, total entities)} from HA's OWN registry.

        Returns BOTH counts because the ratio is the whole signal. A device with 20 of 30
        entities unavailable is demonstrably reachable — the other 10 are reporting — and
        those 20 are optional sensors that are disabled, permission-gated or belong to
        another platform. Only a device where EVERY entity is unavailable has actually
        stopped answering.

        Grouping by entity-name prefix could not see this at all: it split one lock into
        five "devices" and merged unrelated ones sharing a first word. HA's device registry
        is the authority, and `device_id()` / `device_attr()` in a template is the only way
        to reach it over REST — the registry itself is websocket-only.
        """
        if not self.configured:
            raise SourceError("HA_TOKEN not set")
        tpl = (
            "{% set ns = namespace(seen=[]) %}"
            "{% for s in states if s.state == 'unavailable' %}"
            "{% set d = device_id(s.entity_id) %}"
            "{% if d and d not in ns.seen %}{% set ns.seen = ns.seen + [d] %}"
            "{{ device_attr(d, 'name_by_user') or device_attr(d, 'name') }}"
            "|{{ device_entities(d) | select('is_state', 'unavailable') | list | length }}"
            "|{{ device_entities(d) | length }}\n"
            "{% endif %}{% endfor %}"
        )
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as c:
            r = await c.post(f"{self.cfg.ha_url}/api/template",
                             headers={"Authorization": f"Bearer {self.cfg.ha_token}"},
                             json={"template": tpl})
            r.raise_for_status()
            out: dict[str, tuple[int, int]] = {}
            for line in r.text.splitlines():
                parts = line.strip().split("|")
                if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                    continue
                name = parts[0].strip()
                if name and name != "None":
                    out[name] = (int(parts[1]), int(parts[2]))
            return out

    async def device_map(self) -> dict[str, str]:
        """entity_id -> device_id, straight from Home Assistant's device registry.

        This is what makes liveness correct. Grouping entities by name is a guess
        that fails silently: measured here on 2026-09-07, one Nest Protect produced
        23 keys for its 23 entities and a Sonos produced 9 for 11. Every stray key
        is a one-entity phantom device that no sibling can refresh, so a channel at
        a constant value reads "stopped reporting" forever.

        The registry itself is websocket-only, so `device_id()` in a template is the
        only route over REST — the same trick `unavailable_devices` uses. One call
        covers the whole house: measured 2129 entities, 148 KB, 0.02 s, which is
        cheap enough for the slow loop and far too cheap to bother paginating.
        """
        if not self.configured:
            raise SourceError("HA_TOKEN not set")
        tpl = ("{% for s in states %}{{ s.entity_id }}~{{ device_id(s.entity_id) or '' }}"
               "\n{% endfor %}")
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as c:
            r = await c.post(f"{self.cfg.ha_url.rstrip('/')}/api/template",
                             headers={"Authorization": f"Bearer {self.cfg.ha_token}"},
                             json={"template": tpl})
            r.raise_for_status()
            body = r.text
        out: dict[str, str] = {}
        for line in body.splitlines():
            eid, sep, dev = line.strip().partition("~")
            dev = dev.strip()
            # Entities with no device (template sensors, helpers) are left out
            # entirely so device_key falls back to the name heuristic for them.
            if sep and eid and dev and dev.lower() != "none":
                out[eid] = dev
        return out

    async def media_kinds(self) -> dict[str, str]:
        """Map each media_player entity to the integration behind it.

        Sonos, Apple TV and Xbox all present as `media_player.*` with no field
        saying which is which, and the entity ids don't tell you either
        (`media_player.basement` is an Apple TV, `media_player.basement_2` a
        Sonos). Only the integration registry knows, and it is reachable through
        the template API — cheap enough to refresh on the slow loop.
        """
        if not self.configured:
            raise SourceError("HA_TOKEN not set")
        tmpl = ("{% for i in ['sonos','apple_tv','xbox','playstation_network',"
                "'cast','dlna_dmr'] %}"
                "{% for e in integration_entities(i) if e.startswith('media_player.') %}"
                "{{ e }}={{ i }}\n{% endfor %}{% endfor %}")
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as c:
            r = await c.post(
                f"{self.cfg.ha_url.rstrip('/')}/api/template",
                json={"template": tmpl},
                headers={"Authorization": f"Bearer {self.cfg.ha_token}"},
            )
            r.raise_for_status()
            body = r.text
        pretty = {"apple_tv": "Apple TV", "sonos": "Sonos", "xbox": "Xbox",
                  "playstation_network": "PlayStation",
                  "cast": "Cast", "dlna_dmr": "DLNA"}
        out: dict[str, str] = {}
        for line in body.splitlines():
            if "=" in line:
                ent, integ = line.strip().split("=", 1)
                out[ent] = pretty.get(integ, integ)
        return out

    async def media_areas(self) -> dict[str, str]:
        """Map each media_player entity to its Home Assistant area.

        The area is the *location* ("Office", "Primary Bedroom") as distinct from the
        device type, which media_kinds supplies. Friendly names conflate the two and
        sometimes contradict them -- `media_player.sams_bedroom` is a Sonos whose area
        is Office -- so the registry is the only trustworthy source for where a thing is.

        Not every player has one: a roaming speaker legitimately belongs to no room, and
        those come back empty rather than guessed.
        """
        if not self.configured:
            raise SourceError("HA_TOKEN not set")
        # Also covers the console "<name>_now_playing" sensors, which are plain
        # sensors rather than media_players but still name a room.
        tmpl = ("{% for e in states.media_player %}"
                "{{ e.entity_id }}={{ area_name(e.entity_id) or '' }}\n{% endfor %}"
                "{% for e in states.sensor if e.entity_id.endswith('_now_playing') %}"
                "{{ e.entity_id }}={{ area_name(e.entity_id) or '' }}\n{% endfor %}")
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as c:
            r = await c.post(
                f"{self.cfg.ha_url.rstrip('/')}/api/template",
                json={"template": tmpl},
                headers={"Authorization": f"Bearer {self.cfg.ha_token}"},
            )
            r.raise_for_status()
            body = r.text
        out: dict[str, str] = {}
        for line in body.splitlines():
            if "=" in line:
                ent, area = line.strip().split("=", 1)
                if area:
                    out[ent] = area
        return out


def parse_ha_time(value: str | None) -> float | None:
    """HA timestamps are ISO-8601 with offset; return epoch seconds."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def num(state: dict | None) -> float | None:
    if not state:
        return None
    try:
        return float(state["state"])
    except (TypeError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Proxmox VE
# ---------------------------------------------------------------------------
class Proxmox:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @property
    def configured(self) -> bool:
        return bool(self.cfg.pve_token_id and self.cfg.pve_token_secret)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.cfg.http_timeout,
            verify=self.cfg.pve_verify_tls,
            headers={
                "Authorization": (
                    f"PVEAPIToken={self.cfg.pve_token_id}={self.cfg.pve_token_secret}"
                )
            },
        )

    async def _get(self, c: httpx.AsyncClient, path: str) -> Any:
        r = await c.get(f"{self.cfg.pve_host.rstrip('/')}/api2/json{path}")
        r.raise_for_status()
        return r.json().get("data")

    async def collect(self) -> dict:
        """Nodes, guests, storage, pending updates and backup freshness."""
        if not self.configured:
            raise SourceError("PVE token not set")
        out: dict[str, Any] = {"nodes": [], "guests": [], "apt": {}, "backups": {}, "storage": []}
        async with self._client() as c:
            out["nodes"] = await self._get(c, "/nodes") or []
            out["guests"] = await self._get(c, "/cluster/resources?type=vm") or []
            # A stopped guest only matters if it was MEANT to be running. `onboot` is
            # that signal, and it lives only on the per-guest config endpoint --
            # /cluster/resources does not carry it. Fetched for stopped guests ONLY,
            # so this is a couple of extra calls, not one per guest.
            for g in out["guests"]:
                if g.get("status") == "running" or g.get("template"):
                    continue
                kind = "lxc" if str(g.get("type")) in ("lxc", "ct") else "qemu"
                try:
                    conf = await self._get(
                        c, f"/nodes/{g['node']}/{kind}/{g['vmid']}/config") or {}
                    # Absent means Proxmox's default, which is 0 — not set to autostart.
                    g["onboot"] = int(conf.get("onboot") or 0)
                except Exception as exc:  # noqa: BLE001
                    # Leave it unset. Unknown intent must NOT read as "deliberate" —
                    # a lookup failure should make the guest louder, not quieter.
                    log.warning("onboot lookup failed for %s: %s", g.get("vmid"), exc)
            try:
                out["storage"] = await self._get(c, "/cluster/resources?type=storage") or []
            except Exception as exc:  # noqa: BLE001
                log.warning("storage query failed: %s", exc)

            for node in self.cfg.pve_nodes:
                # NB: pending apt updates are NOT read here. That endpoint
                # (/nodes/{node}/apt/update) requires Sys.Modify — a write
                # permission this read-only token deliberately lacks — and it
                # only covers hosts anyway. Patch debt arrives instead via
                # POST /api/ingest/patch, pushed by sagamore-patch-push.sh on
                # pve, which covers hosts *and* every container. Calling it here
                # would just log a 403 every minute.

                # Most recent backup task, so "backups are running" is evidence-based.
                try:
                    tasks = await self._get(
                        c, f"/nodes/{node}/tasks?typefilter=vzdump&limit=1"
                    ) or []
                    if tasks:
                        t = tasks[0]
                        out["backups"][node] = {
                            "status": t.get("status", "running"),
                            "starttime": t.get("starttime"),
                        }
                    else:
                        out["backups"][node] = None
                except Exception as exc:  # noqa: BLE001
                    log.warning("task query failed on %s: %s", node, exc)
                    out["backups"][node] = None
        return out


# ---------------------------------------------------------------------------
# UniFi
# ---------------------------------------------------------------------------
async def _unifi_get(cfg, path: str) -> list:
    url = f"{cfg.unifi_host.rstrip('/')}/proxy/network/api/s/{cfg.unifi_site}/{path}"
    async with httpx.AsyncClient(timeout=cfg.http_timeout, verify=False) as c:
        r = await c.get(url, headers={"X-API-KEY": cfg.unifi_api_key,
                                      "Accept": "application/json"})
        r.raise_for_status()
        return (r.json() or {}).get("data") or []


async def _unifi_post(cfg, path: str, body: dict) -> list:
    """POST twin of _unifi_get. The `stat/report/*` rollups take a time window in the
    body and are POST-only; a GET returns an empty set rather than an error, which is
    how this silently returned nothing the first time."""
    url = f"{cfg.unifi_host.rstrip('/')}/proxy/network/api/s/{cfg.unifi_site}/{path}"
    async with httpx.AsyncClient(timeout=cfg.http_timeout, verify=False) as c:
        r = await c.post(url, json=body,
                         headers={"X-API-KEY": cfg.unifi_api_key,
                                  "Accept": "application/json"})
        r.raise_for_status()
        return (r.json() or {}).get("data") or []


async def unifi_health(cfg) -> dict:
    """Per-subsystem health: wan, www, wlan, lan, vpn -- keyed by subsystem name."""
    if not cfg.unifi_api_key:
        raise SourceError("UNIFI_API_KEY not set")
    return {x.get("subsystem"): x for x in await _unifi_get(cfg, "stat/health")
            if isinstance(x, dict) and x.get("subsystem")}


async def unifi_ap_satisfaction(cfg, hours: int = 3) -> dict[str, float]:
    """{AP name: satisfaction averaged over the last few HOURS}, from UniFi's own rollup.

    🚨 WHY THIS EXISTS. `stat/device` gives one INSTANTANEOUS satisfaction figure per
    radio, and the slow poll reads it once every 15 minutes. A single sample is not a
    condition: on 2026-09-08 Primary Bedroom 5 GHz was captured at 88% airtime and 60%
    satisfaction, the card went red -- and UniFi's hourly rollup for that same AP never
    left the 95-97 band all day, before or after. The day before, Upstairs Hallway 2.4
    did the same thing and had recovered by the time anyone looked.

    `stat/report/hourly.ap` is the aggregate over every client for the whole hour, which
    is what "are people actually suffering" means. Per-radio instantaneous numbers stay
    on the card as detail; they just stop deciding its colour.

    ⚠️ Per AP, not per radio -- the rollup carries no band. That is the right grain for
    "is this access point degraded" and the wrong one for "which band", so the table
    keeps showing both bands separately.
    """
    if not cfg.unifi_api_key:
        raise SourceError("UNIFI_API_KEY not set")
    names = {d.get("mac"): d.get("name") for d in await _unifi_get(cfg, "stat/device")
             if isinstance(d, dict) and d.get("type") == "uap"}
    end = int(time.time() * 1000)
    body = {"start": end - hours * 3600 * 1000, "end": end,
            "attrs": ["time", "satisfaction", "num_sta"]}
    rows = await _unifi_post(cfg, "stat/report/hourly.ap", body)
    acc: dict[str, list[float]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = names.get(r.get("ap"))
        sat = r.get("satisfaction")
        # A zero-client hour reports satisfaction 0 and would drag a healthy AP under
        # the threshold on its own. No clients is not bad wifi.
        if name and isinstance(sat, (int, float)) and (r.get("num_sta") or 0) > 0:
            acc.setdefault(name, []).append(float(sat))
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


async def unifi_devices(cfg) -> list[dict]:
    """APs, switches and the gateway, with per-radio airtime figures.

    Channel utilisation is the number that actually predicts bad wifi here, and it is
    per radio rather than per AP -- 2.4 GHz can be congested while 5 GHz beside it is
    idle, and an AP-level average hides exactly that.
    """
    if not cfg.unifi_api_key:
        raise SourceError("UNIFI_API_KEY not set")
    out = []
    for x in await _unifi_get(cfg, "stat/device"):
        if not isinstance(x, dict):
            continue
        radios = []
        for r in (x.get("radio_table_stats") or []):
            tot = r.get("cu_total")
            ours = (r.get("cu_self_rx") or 0) + (r.get("cu_self_tx") or 0)
            radios.append({
                "band": "5 GHz" if r.get("radio") == "na" else "2.4 GHz",
                "channel": r.get("channel"),
                # cu_total is ALL airtime in use. On its own it says nothing about
                # health: a radio at 88% because someone is pulling a large download
                # is a radio doing its job. What hurts is airtime we do NOT control.
                "util": tot,
                "ours": ours,
                "interference": (None if tot is None else max(0, tot - ours)),
                # UniFi's own client-experience score, and the only figure here that
                # reflects what using the wifi actually feels like.
                "satisfaction": r.get("satisfaction"),
                "clients": r.get("user-num_sta"),
            })
        out.append({
            "name": x.get("name") or x.get("mac", ""),
            "type": x.get("type", ""),
            "state": x.get("state"),          # 1 = connected
            "uptime": x.get("uptime") or 0,
            "clients": x.get("num_sta"),
            "version": x.get("version", ""),
            "upgradable": bool(x.get("upgradable")),
            "radios": radios,
        })
    return out


async def unifi_clients(cfg) -> list[dict]:
    """Every client the controller currently sees, normalised.

    Uses the controller's own client list rather than Home Assistant's UniFi
    device_trackers. HA only creates trackers for entities someone enabled -- 30 of
    them here against 97 real clients -- so a count derived from HA answers "how many
    of the devices I once ticked are home", which is not a question anyone asks and
    reads like a network inventory when it is not.

    The v1 integration API returns clients but not which network each is on, which is
    the whole point here, so this uses the controller API behind the same UniFi OS
    proxy. Both accept the same X-API-KEY.
    """
    if not cfg.unifi_api_key:
        raise SourceError("UNIFI_API_KEY not set")
    data = await _unifi_get(cfg, "stat/sta")

    out = []
    for x in data:
        if not isinstance(x, dict):
            continue
        out.append({
            "mac": x.get("mac", ""),
            "ip": x.get("ip", ""),
            # `name` is what someone typed in the UniFi UI. Its presence is the only
            # signal available for "a human has looked at this and knows what it is";
            # hostname and OUI are what the device says about itself, which an unknown
            # device also has.
            "named": bool(x.get("name")),
            "label": x.get("name") or x.get("hostname") or x.get("oui") or x.get("mac", ""),
            "oui": x.get("oui", ""),
            "network": x.get("network") or "",
            "essid": x.get("essid") or "",
            "wired": bool(x.get("is_wired")),
            "guest": bool(x.get("is_guest")),
            "first_seen": x.get("first_seen") or 0,
            "last_seen": x.get("last_seen") or 0,
        })
    return out


# ---------------------------------------------------------------------------
# TLS expiry
# ---------------------------------------------------------------------------
def cert_days_remaining(host: str, port: int = 443, timeout: int = 8) -> int | None:
    """Days until the served certificate expires. None if it can't be read."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
        # Parse notAfter without pulling in a crypto dependency.
        import subprocess

        proc = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-noout", "-enddate"],
            input=der, capture_output=True, timeout=timeout,
        )
        line = proc.stdout.decode().strip()
        if not line.startswith("notAfter="):
            return None
        exp = dt.datetime.strptime(line.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z")
        return (exp - dt.datetime.utcnow()).days
    except Exception as exc:  # noqa: BLE001
        log.warning("cert check failed for %s: %s", host, exc)
        return None


# ---------------------------------------------------------------------------
# Nextcloud Bookmarks (bookmark store + Floccus backend)
# ---------------------------------------------------------------------------
class NextcloudBookmarks:
    """The single bookmark store.

    Replaced Linkwarden on 2026-09-01: Nextcloud was already running for Office,
    so this collapses a whole service rather than adding one, and Floccus speaks
    to it just as well. See docs/bookmarks.md.

    ⚠️ The REST API is **v2**. The v1 path still exists in old docs and returns a
    404 *HTML page* rather than JSON, so a mistake there surfaces as a confusing
    parse error rather than a clean 404.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @property
    def configured(self) -> bool:
        return bool(self.cfg.nextcloud_url and self.cfg.nextcloud_user
                    and self.cfg.nextcloud_password)

    @property
    def _base(self) -> str:
        root = self.cfg.nextcloud_url.rstrip("/")
        return f"{root}/index.php/apps/bookmarks/public/rest/v2"

    def _auth(self) -> tuple[str, str]:
        return (self.cfg.nextcloud_user, self.cfg.nextcloud_password)

    async def _folder_names(self, c: httpx.AsyncClient) -> dict[int, str]:
        """id -> title, so a bookmark's folder id can be shown as its name."""
        r = await c.get(f"{self._base}/folder", auth=self._auth())
        r.raise_for_status()
        return {f["id"]: (f.get("title") or "Unsorted")
                for f in (r.json().get("data") or [])}

    async def _folder_id(self, c: httpx.AsyncClient, name: str | None = None) -> int:
        """Resolve a folder by name, creating it on first use.

        Falls back to the configured Inbox, which is where captures land by design.
        Matching is case-insensitive so "inbox" and "Inbox" do not become two folders.
        """
        target = (name or "").strip() or self.cfg.nextcloud_inbox
        for fid, title in (await self._folder_names(c)).items():
            if title.strip().lower() == target.lower():
                return fid
        r = await c.post(f"{self._base}/folder", auth=self._auth(),
                         json={"title": target, "parent_folder": -1})
        r.raise_for_status()
        return r.json()["item"]["id"]

    async def save(self, url: str, title: str, tags: list[str],
                   folder: str | None = None) -> None:
        """Create a bookmark. Without a folder it lands in the Inbox."""
        if not self.configured:
            raise SourceError("nextcloud bookmarks not configured")
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as c:
            payload: dict[str, Any] = {
                "url": url,
                "tags": [t for t in tags if t],
                "folders": [await self._folder_id(c, folder)],
            }
            if title:
                payload["title"] = title
            r = await c.post(f"{self._base}/bookmark", auth=self._auth(), json=payload)
            r.raise_for_status()

    async def fetch(self) -> list[dict]:
        """Every bookmark.

        `page=-1` returns the whole set in one response, so unlike Linkwarden --
        whose `take` silently capped at 50 and looked like data loss -- there is
        no cursor to walk and nothing to truncate.
        """
        if not self.configured:
            raise SourceError("nextcloud bookmarks not configured")
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as c:
            names = await self._folder_names(c)
            r = await c.get(f"{self._base}/bookmark?page=-1", auth=self._auth())
            r.raise_for_status()
            out: list[dict] = []
            for b in (r.json().get("data") or []):
                fids = b.get("folders") or []
                out.append({
                    "url": b.get("url", ""),
                    "title": b.get("title") or b.get("url", ""),
                    "folder": names.get(fids[0], "Unsorted") if fids else "Unsorted",
                    "notes": b.get("description") or "",
                    # Lower-cased and comma-joined. Stored that way so a lookup can
                    # test for an exact tag rather than a substring -- "favorite"
                    # must not be matched by "favorites-old".
                    "tags": ",".join(sorted({str(t).strip().lower()
                                             for t in (b.get("tags") or [])
                                             if str(t).strip()})),
                })
            return out


# ---------------------------------------------------------------------------
# Netscape bookmark-file import (what every browser exports)
# ---------------------------------------------------------------------------
def parse_netscape_bookmarks(html: str) -> list[dict]:
    """Parse a browser bookmarks export.

    Safari, Chrome, Firefox and Edge all export this same 1994 Netscape format,
    which is why it's the import path: one file works for every browser.
    Folder nesting is flattened to the nearest enclosing <H3>.
    """
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.out: list[dict] = []
            self.stack: list[str] = []
            self._href: str | None = None
            self._buf: list[str] = []
            self._in_h3 = False

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "h3":
                self._in_h3 = True
                self._buf = []
            elif tag == "a" and a.get("href"):
                self._href = a["href"]
                self._buf = []
            elif tag == "dl":
                self.stack.append(self._pending or "")
                self._pending = ""

        def handle_endtag(self, tag):
            if tag == "h3":
                self._in_h3 = False
                self._pending = "".join(self._buf).strip()
            elif tag == "a" and self._href:
                title = "".join(self._buf).strip() or self._href
                folder = next((f for f in reversed(self.stack) if f), "Unsorted")
                if self._href.startswith(("http://", "https://")):
                    self.out.append({"url": self._href, "title": title, "folder": folder})
                self._href = None
            elif tag == "dl" and self.stack:
                self.stack.pop()

        def handle_data(self, data):
            if self._in_h3 or self._href:
                self._buf.append(data)

        _pending = ""

    p = P()
    p.feed(html)
    # De-duplicate on URL, keeping the first (shallowest) occurrence.
    seen: set[str] = set()
    uniq: list[dict] = []
    for b in p.out:
        if b["url"] in seen:
            continue
        seen.add(b["url"])
        uniq.append(b)
    return uniq


# ---------------------------------------------------------------------------
# PlayStation (DDP — Device Discovery Protocol)
# ---------------------------------------------------------------------------
def discover_playstations(timeout: int = 3) -> list[dict]:
    """Find PlayStations on the LAN and read their firmware.

    Worth contrasting with the Xbox: Sony's DDP discovery returns
    `system-version` outright, so a PlayStation's firmware IS locally readable.
    Microsoft exposes no equivalent (see `panels.consoles_panel`).

    Consoles answer while in rest mode; a fully powered-off console is silent,
    which is indistinguishable from absent — so a missing console is reported as
    "not responding", never as a version we last saw.

    **Storage is not obtainable, and this is the complete evidence.** The whole
    response is seven fields — verified against the live PS5 on 2026-08-21:

        HTTP/1.1 620 Server Standby
        host-id / host-type / host-name / host-request-port
        device-discovery-protocol-version / system-version

    No capacity, and in standby the console opens no TCP port at all. Remote Play
    (9295-9297) needs PIN pairing and carries no storage either, and Sony
    publishes no console API. So the pair invert each other: the Xbox reports
    storage but never firmware (via the Xbox Live cloud that HA already uses),
    and the PS5 reports firmware but never storage.

    What the response *does* carry beyond the version is the HTTP status line —
    `620 Server Standby` vs `200 Ok`. That is all.

    It does NOT carry the running title. Captured on 2026-08-30 with a game actually
    running, an awake PS5 (firmware 13.60) returns exactly seven fields and none of
    them is `running-app-name`. An earlier version of this docstring claimed the title
    was included when awake; that came from PS4 behaviour and was never tested against
    a PS5 mid-game. `running_app` therefore stays "" on this console.
    """
    import socket

    found: dict[str, dict] = {}
    probe = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"
    for port in (987, 9302):          # PS4 and PS5 respectively
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        try:
            s.sendto(probe, ("255.255.255.255", port))
        except OSError as exc:
            log.warning("DDP send failed on %s: %s", port, exc)
            continue
        deadline = dt.datetime.now().timestamp() + timeout
        while dt.datetime.now().timestamp() < deadline:
            try:
                data, addr = s.recvfrom(2048)
            except (TimeoutError, OSError):
                break
            body = data.decode(errors="ignore")
            lines = body.splitlines()
            fields = {}
            for line in lines:
                if ":" in line:
                    k, v = line.split(":", 1)
                    fields[k.strip().lower()] = v.strip()
            # The first line is an HTTP-ish status: "200 Ok" when awake,
            # "620 Server Standby" in rest mode. It is the only power signal.
            status_line = lines[0] if lines else ""
            awake = " 200 " in f" {status_line} " or status_line.strip().endswith("Ok")
            if "host-type" in fields:
                found[addr[0]] = {
                    "ip": addr[0],
                    "host_type": fields.get("host-type", "?"),
                    "name": fields.get("host-name", addr[0]),
                    "raw_version": fields.get("system-version", ""),
                    "version": decode_ps_version(fields.get("system-version", "")),
                    "awake": awake,
                    "status": status_line.replace("HTTP/1.1", "").strip(),
                    "running_app": fields.get("running-app-name", ""),
                }
        s.close()
    return list(found.values())


def decode_ps_version(raw: str) -> str:
    """`13600007` -> `13.60`.

    Sony packs the firmware as MMmmbbbb — major, minor, build. Only the first
    four digits are the version people recognise; the build suffix is noise.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) < 4:
        return ""
    return f"{int(digits[:2])}.{digits[2:4]}"

# ---------------------------------------------------------------------------
# tvOS app artwork (Apple iTunes Search API)
# ---------------------------------------------------------------------------
# An Apple TV playing a live channel reports an app but NO entity_picture, so those
# rows were the only ones on the Now Playing card with nothing on the right. The app
# icon is the honest stand-in: it says "this is YouTube TV" without pretending to be
# artwork for the programme.
#
# `lookup` takes the bundle id straight from the media_player's `app_id` and needs no
# key. entity=tvSoftware matters -- without it Apple answers with the iOS build, whose
# icon can differ from the tvOS one.
#
# Cached on disk and effectively permanently: an app icon changes maybe once a year,
# and Apple rate-limits this endpoint (~20 requests/minute). Misses are cached too, for
# a shorter time, so an app that simply is not in the store is not retried every poll.
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
ICON_TTL = 30 * 24 * 3600
ICON_MISS_TTL = 3 * 24 * 3600
ICON_MAX_PER_POLL = 5          # stay far under Apple's rate limit


async def resolve_app_icons(states: dict, cache_path) -> dict[str, str]:
    """bundle id -> tvOS icon URL, for media players that report an app but no art."""
    from pathlib import Path
    cache_path = Path(cache_path)
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:
        cache = {}

    wanted: set[str] = set()
    for st in (states or {}).values():
        a = st.get("attributes") or {}
        if a.get("app_id") and not a.get("entity_picture"):
            wanted.add(a["app_id"])

    now = time.time()
    todo = [b for b in wanted
            if now - cache.get(b, {}).get("at", 0) > (ICON_TTL if cache.get(b, {}).get("url") else ICON_MISS_TTL)]
    if todo:
        async with httpx.AsyncClient(timeout=10) as client:
            for bundle in sorted(todo)[:ICON_MAX_PER_POLL]:
                url = None
                try:
                    r = await client.get(ITUNES_LOOKUP,
                                         params={"bundleId": bundle, "entity": "tvSoftware",
                                                 "country": "US"})
                    res = (r.json() or {}).get("results") or []
                    if res:
                        url = res[0].get("artworkUrl512") or res[0].get("artworkUrl100")
                except Exception as exc:  # noqa: BLE001
                    log.info("app icon lookup failed for %s: %s", bundle, exc)
                    continue          # no cache entry, so a transient failure retries
                cache[bundle] = {"url": url, "at": now}
        try:
            cache_path.write_text(json.dumps(cache))
        except OSError as exc:
            log.warning("could not write app icon cache: %s", exc)

    return {b: v["url"] for b, v in cache.items() if v.get("url")}
