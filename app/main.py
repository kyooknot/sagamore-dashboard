"""Sagamore — the instrumented house.

A state dashboard, not a link grid. Every tile answers two questions at once:
what is this thing doing, and how sure are we that we can still see it.

Named for Sagamore Hill, Roosevelt's house at Oyster Bay — the "Summer White
House", the seat of government wherever he happened to be. Which is the point of
a dashboard you read from anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import re

import httpx
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import panels as P
from .config import CONFIG
from .db import Database
from .liveness import set_device_registry
from .model import Status, worst
from .updates import carry_app_first_seen, carry_pause_since, carry_patch_first_seen
from .sources import (
    HomeAssistant,
    NextcloudBookmarks,
    Proxmox,
    SourceError,
    cert_days_remaining,
    discover_playstations,
    num,
    parse_netscape_bookmarks,
    resolve_app_icons,
    unifi_clients,
    unifi_devices,
    unifi_ap_satisfaction,
    unifi_health,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("sagamore")

HERE = Path(__file__).parent
app = FastAPI(title="Sagamore", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _asset_version() -> str:
    """Cache-buster from the static files' mtimes.

    The stylesheet was linked bare, so a browser that had ever loaded the page kept
    serving the old file — a CSS fix could be correct on the server and invisible in
    the room. Any future edit now changes the URL automatically.

    app.js was left out of that fix and then linked bare itself, which broke the manual
    refresh button in exactly the way described above: the endpoint answered fine, the
    handler that calls it was never in the browser's cached copy, and nothing appeared
    wrong from the server side. Every versioned asset must be covered here.
    """
    stamp = 0
    for name in ("app.css", "app.js"):
        try:
            stamp = max(stamp, int((Path(__file__).parent / "static" / name).stat().st_mtime))
        except OSError:
            pass
    return str(stamp)


def _tpl_host(url: str) -> str:
    """Bare hostname from a bookmark URL, for the favicon endpoint."""
    try:
        return (url or "").split("//", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0]
    except (IndexError, AttributeError):
        return ""


templates.env.filters["host"] = _tpl_host

db = Database(CONFIG.db_path)
ha = HomeAssistant(CONFIG)
pve = Proxmox(CONFIG)
nextcloud = NextcloudBookmarks(CONFIG)

# In-memory latest view. The DB snapshot is the cold-start fallback so a restart
# renders instantly instead of showing an empty page until the first poll.
STATE: dict = {"states": {}, "pve": None, "cert_days": None, "polled_at": None,
               "errors": {}, "playstations": [], "media_kinds": {},
                 "app_icons": {}, "media_areas": {}, "unifi": None,
                 "unifi_health": {}, "unifi_devices": []}


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
async def poll_fast() -> None:
    """Home Assistant + Proxmox. One failing source must not take the other."""
    errors: dict[str, str] = {}

    try:
        STATE["states"] = await ha.states()
    except (SourceError, Exception) as exc:  # noqa: BLE001
        errors["homeassistant"] = str(exc)[:200]
        log.warning("HA poll failed: %s", exc)

    try:
        STATE["pve"] = await pve.collect()
    except (SourceError, Exception) as exc:  # noqa: BLE001
        errors["proxmox"] = str(exc)[:200]
        log.warning("PVE poll failed: %s", exc)

    if CONFIG.unifi_api_key:
        try:
            STATE["unifi"] = await unifi_clients(CONFIG)
            # Small payload, and "the internet is down" is not a fact worth learning
            # fifteen minutes late. The per-AP device stats are heavier and change
            # slowly, so they ride the slow loop instead.
            STATE["unifi_health"] = await unifi_health(CONFIG)
        except Exception as exc:  # noqa: BLE001
            # Leave the last good list in place but let the panel go stale rather than
            # blanking the section on one failed request.
            errors["unifi"] = str(exc)[:200]
            log.warning("UniFi poll failed: %s", exc)

    STATE["errors"] = errors
    STATE["polled_at"] = time.time()

    # How long has each paused player been paused? Our clock, not HA's: a restart or an
    # integration reload rewrites `last_changed` AND `media_position_updated_at` for every
    # entity, so HA cannot date a pause at all (see carry_pause_since). Kept in a snapshot
    # rather than memory so a Sagamore restart does not hand every paused device a fresh
    # 20 minutes on the card.
    _states = STATE.get("states") or {}
    paused_now = {
        eid: f"{(st.get('attributes') or {}).get('media_title')}"
             f"|{(st.get('attributes') or {}).get('media_position')}"
        for eid, st in _states.items()
        if eid.startswith("media_player.") and st.get("state") == "paused"
    }
    # Seed a pause we have not seen before from HA's own timestamp, not from now —
    # otherwise the first poll after a deploy grants every standing pause a fresh grace.
    from .sources import parse_ha_time as _pt
    _seed = {}
    for eid in paused_now:
        st = _states.get(eid) or {}
        t = _pt((st.get("attributes") or {}).get("media_position_updated_at")) \
            or _pt(st.get("last_changed"))
        if t:
            _seed[eid] = t
    _prev = ((db.get_snapshot("pause_since") or (0, {}))[1] or {}).get("players") or {}
    _pause = carry_pause_since(_prev, paused_now, time.time(), _seed)
    STATE["pause_since"] = _pause
    if _pause != _prev:
        db.put_snapshot("pause_since", {"players": _pause})

    # Persist the numbers worth trending.
    states = STATE.get("states") or {}
    points = {}
    for eid, st in states.items():
        a = st.get("attributes", {})
        if a.get("device_class") == "power" and a.get("unit_of_measurement") == "W":
            v = num(st)
            if v is not None:
                points[f"power:{eid}"] = v
    for key, eid in (
        ("grid:used", "sensor.national_grid_current_bill_electric_usage_to_date"),
        ("grid:forecast", "sensor.national_grid_current_bill_electric_forecasted_usage"),
    ):
        v = num(states.get(eid))
        if v is not None:
            points[key] = v
    if points:
        db.record(points)

    db.put_snapshot("last", {"polled_at": STATE["polled_at"], "errors": errors})


async def poll_slow() -> None:
    """Things that change on the order of days."""
    STATE["cert_days"] = await asyncio.to_thread(cert_days_remaining, CONFIG.cert_host)

    if CONFIG.unifi_api_key:
        try:
            STATE["unifi_devices"] = await unifi_devices(CONFIG)
            # Sustained per-AP satisfaction, which is what decides a radio's state.
            # Failing here is not fatal: network_panel falls back to the instantaneous
            # per-radio sample, which is what shipped before.
            try:
                STATE["ap_satisfaction"] = await unifi_ap_satisfaction(CONFIG)
            except Exception as exc:  # noqa: BLE001
                log.warning("UniFi hourly satisfaction failed, using live samples: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("UniFi device poll failed: %s", exc)
    # DDP is a broadcast probe; a console in rest mode answers, one fully
    # powered off is silent. Never cache a stale version for a silent console.
    try:
        # Install HA's real entity -> device map before anything reads liveness.
        # A failure here is not fatal: device_key falls back to its name heuristic,
        # which is what shipped for months -- degraded, not broken.
        try:
            set_device_registry(await ha.device_map())
        except Exception as exc:  # noqa: BLE001
            log.warning("device registry fetch failed, keeping name heuristic: %s", exc)
        STATE["media_kinds"] = await ha.media_kinds()
        STATE["media_areas"] = await ha.media_areas()
        # tvOS app icons for players that report an app but no artwork. Disk-cached,
        # so this is a no-op on almost every poll.
        try:
            STATE["app_icons"] = await resolve_app_icons(
                STATE.get("states") or {},
                Path(CONFIG.db_path).with_name("appicons.json"))
        except Exception as exc:  # noqa: BLE001
            log.info("app icon resolve skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("media kind lookup failed: %s", exc)
    try:
        STATE["dead_devices"] = await ha.unavailable_devices()
    except Exception as exc:  # noqa: BLE001
        log.warning("device registry lookup failed: %s", exc)

    try:
        STATE["playstations"] = await asyncio.to_thread(discover_playstations)
    except Exception as exc:  # noqa: BLE001
        log.warning("PlayStation discovery failed: %s", exc)
        STATE["playstations"] = []
    # GameLog exports this hourly for Homepage; we read the file rather than querying RA,
    # Xbox and PSN ourselves — no second set of credentials, no per-refresh upstream call.
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(CONFIG.gamelog_url)
            if r.status_code == 200:
                STATE["gaming"] = (time.time(), r.json())
    except Exception as exc:  # noqa: BLE001
        log.warning("gamelog feed failed: %s", exc)

    # GameVault: what is actually on the shelf. Two calls, because neither endpoint is
    # sufficient alone -- /api/dashboard carries the totals and the pipeline run
    # history, /api/collection the per-item rows the platform breakdown is tallied
    # from (the dashboard's own by_system block returns n=None for every platform).
    # The collection call is allowed to fail on its own: totals without a breakdown is
    # a smaller card, whereas no card at all would be a worse answer than both.
    if CONFIG.gamevault_url:
        try:
            async with httpx.AsyncClient(timeout=10, base_url=CONFIG.gamevault_url) as c:
                dash, coll = await asyncio.gather(
                    c.get("/api/dashboard"), c.get("/api/collection"),
                    return_exceptions=True)
                if getattr(dash, "status_code", 0) == 200:
                    STATE["gamevault"] = (
                        time.time(), dash.json(),
                        coll.json() if getattr(coll, "status_code", 0) == 200 else {})
        except Exception as exc:  # noqa: BLE001
            log.warning("gamevault feed failed: %s", exc)

    if nextcloud.configured:
        try:
            marks = await nextcloud.fetch()
            db.replace_source("nextcloud")
            # Legacy rows from the Linkwarden era. upsert is keyed on url so the
            # migrated ones simply flip source, but anything that only ever lived
            # in Linkwarden would otherwise linger forever. Harmless once empty.
            db.replace_source("linkwarden")
            for m in marks:
                db.upsert_bookmark(m["url"], m["title"], m["folder"],
                                   m.get("notes", ""), "nextcloud", m.get("tags", ""))
            log.info("synced %d bookmarks from Nextcloud", len(marks))
        except Exception as exc:  # noqa: BLE001
            log.warning("Nextcloud bookmark sync failed: %s", exc)


async def _loop(fn, seconds: int, name: str) -> None:
    while True:
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("%s loop error: %s", name, exc)
        await asyncio.sleep(seconds)


@app.on_event("startup")
async def startup() -> None:
    for problem in CONFIG.validate():
        log.warning("config: %s", problem)
    await poll_fast()
    await poll_slow()
    app.state.tasks = [
        asyncio.create_task(_loop(poll_fast, CONFIG.poll_seconds, "fast")),
        asyncio.create_task(_loop(poll_slow, CONFIG.slow_poll_seconds, "slow")),
    ]


@app.on_event("shutdown")
async def shutdown() -> None:
    for t in getattr(app.state, "tasks", []):
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


# ---------------------------------------------------------------------------
# View assembly
# ---------------------------------------------------------------------------
def build_panels() -> list:
    states = STATE.get("states") or {}
    panels = [
        # Now Playing leads: it is the only card that answers "what is happening in the
        # house right now", and it collapses to a single line when nothing is on.
        P.now_playing_panel(states, STATE.get("media_kinds"),
                            STATE.get("app_icons"), STATE.get("media_areas"),
                            ((STATE.get("gaming") or (0, {}))[1] or {}).get("console_progress"),
                            db.get_snapshot("esde"), STATE.get("pause_since")),
        # Trash used to sit here as a card. It is a banner now -- see P.trash_banner --
        # because it carried one date across a full tile and is only actionable for
        # about twelve hours a week.
        P.hvac_panel(states),
        P.power_panel(states, CONFIG, db),
        # Leak Watch is no longer its own card -- water is one of the things Security
        # covers, and leak_readings hands it a headline that outranks "doors closed".
        P.security_panel(states, STATE.get("unifi"), CONFIG),
        P.network_panel(STATE.get("unifi"), STATE.get("unifi_health"),
                        STATE.get("unifi_devices"), CONFIG,
                        STATE.get("ap_satisfaction")),
        # Storage sits with Security and Network Health: three one-column cards, one row.
        P.storage_panel(db.get_snapshot("storage"),
                        (STATE.get("pve") or {}).get("storage")),
        P.gaming_panel(STATE.get("gaming")),
        P.collection_panel(STATE.get("gamevault")),
        P.homelab_panel(STATE.get("pve"), states, STATE.get("cert_days"),
                        db.get_snapshot("patch"), db.get_snapshot("apps"),
                        STATE.get("playstations"), STATE.get("dead_devices"),
                        db.get_snapshot("health")),
    ]
    # Now Playing disappears when nothing is on, the way the trash banner does: it is
    # only ever interesting when the answer is "something", and a permanent "Nothing
    # playing" card is a line of furniture rather than information.
    # ⚠️ Only when the panel is confidently idle. It keeps its place if it carries an
    # error, because a card that vanishes on an outage is indistinguishable from a
    # quiet house — the failure mode this dashboard exists to avoid.
    return [p for p in panels
            if not (p.key == "nowplaying" and not p.rows and not p.error)]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ps = build_panels()
    overall = worst(*[p.rollup() for p in ps])
    asset_v = _asset_version()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "panels": ps,
            "overall": overall,
            "attention": P.attention_items(ps),
            "asset_v": asset_v,
            "Status": Status,
            "polled_at": STATE.get("polled_at"),
            "trash": P.trash_banner(),
            "favourites": P.favourites(db.bookmarks()),
            "errors": STATE.get("errors") or {},
            "bookmarks": _grouped_bookmarks(),
            "bookmark_sync": nextcloud.configured,
            # Stated on the page so the thresholds behind an arrow are visible
            # rather than folded into the code.
            "pct_gate": int(P.TREND_MIN_PCT),
            "watt_gate": int(P.TREND_MIN_WATTS),
        },
    )


def _grouped_bookmarks() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for b in db.bookmarks():
        # Same resolution the favourite tiles use: a mapped product logo first,
        # then the cached favicon. The list used to hard-code the favicon path,
        # so internal services behind Authentik — whose favicon fetch only ever
        # gets a login page — rendered blank even when a logo was mapped.
        b["icon"] = P._icon_for(b["url"])
        grouped.setdefault(b["folder"] or "Unsorted", []).append(b)
    return dict(sorted(grouped.items()))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/state")
async def api_state():
    ps = build_panels()
    return JSONResponse(
        {
            "overall": worst(*[p.rollup() for p in ps]).value,
            "polled_at": STATE.get("polled_at"),
            "errors": STATE.get("errors"),
            "panels": [
                {
                    "key": p.key,
                    "title": p.title,
                    "status": p.rollup().value,
                    "headline": p.headline,
                    "sub": p.sub,
                    "error": p.error,
                    "readings": [
                        {
                            "label": r.label,
                            "value": r.display(),
                            "status": r.effective_status.value,
                            "age": r.age_text,
                            "note": r.note,
                        }
                        for r in p.readings
                    ],
                    # Per-circuit movement, so the off-site health sweep can
                    # notice a load jumping while nobody is in the house.
                    **({"trends": [
                        {"name": c["name"], "pct": round(c["trend"]["pct"], 1)
                         if c["trend"]["pct"] is not None else None,
                         "delta_w": round(c["trend"].get("delta_w") or 0),
                         "state": c["trend"]["state"]}
                        for c in p.rows if c.get("trend", {}).get("state") in ("up", "down")
                    ]} if p.key == "power" else {}),
                }
                for p in ps
            ],
        }
    )


_refresh_lock = asyncio.Lock()


@app.post("/api/refresh")
async def api_refresh():
    """Collect now, instead of waiting out the poll interval.

    Deliberately the same code path as the scheduled poll rather than a second one --
    a refresh button that fetched differently from the poller would be a button that
    lies. The lock means a double-click waits for the run already in flight rather than
    starting a competing one.
    """
    async with _refresh_lock:
        await poll_fast()
    return JSONResponse({"ok": True, "polled_at": STATE.get("polled_at")})


@app.get("/api/health")
async def api_health():
    """Liveness for the homelab's own monitoring."""
    stale = (
        STATE.get("polled_at") is None
        or (time.time() - STATE["polled_at"]) > CONFIG.poll_seconds * 5
    )
    return JSONResponse(
        {"ok": not stale, "polled_at": STATE.get("polled_at"), "errors": STATE.get("errors")},
        status_code=200 if not stale else 503,
    )


@app.get("/api/series/{key:path}")
async def api_series(key: str, hours: int = 24):
    since = int(time.time()) - hours * 3600
    return JSONResponse({"key": key, "points": db.series_since(key, since)})


# ---------------------------------------------------------------------------
# Ingest — pve pushes its patch report here
# ---------------------------------------------------------------------------
@app.post("/api/ingest/patch")
async def ingest_patch(request: Request, authorization: str = Header(default="")):
    """Accept the patch-debt report pushed by pve.

    Push, not pull, on purpose: reading pending updates through the Proxmox API
    needs Sys.Modify — a write permission a read-only dashboard has no business
    holding. pve already knows the answer for hosts *and* every container, so it
    reports in, the same way the Mac and the Windows PC already do.
    """
    if not CONFIG.ingest_token:
        raise HTTPException(503, "ingest not configured")
    if authorization.removeprefix("Bearer ").strip() != CONFIG.ingest_token:
        raise HTTPException(401, "bad ingest token")
    body = await request.json()
    targets = body.get("targets")
    if not isinstance(targets, list):
        raise HTTPException(400, "expected {'targets': [...]}")
    # First-seen stamps carry over from the previous push, and `routine` says when the last
    # 05:00 run ran: together they tell "applies tonight" from "a run came and went without
    # applying it" (app/updates.py). Both added 2026-09-11.
    prev = (db.get_snapshot("patch") or (0, {}))[1] or {}
    carry_patch_first_seen(prev.get("targets"), targets, time.time())
    db.put_snapshot("patch", {"targets": targets, "source": body.get("source", "pve"),
                              "routine": body.get("routine")})
    db.log_event("ingest.patch", f"{len(targets)} targets")
    log.info("patch report ingested: %d targets", len(targets))
    return JSONResponse({"ok": True, "targets": len(targets)})


@app.post("/api/ingest/storage")
async def ingest_storage(request: Request, authorization: str = Header(default="")):
    """Accept the ZFS dataset report pushed by the storage host.

    The Proxmox API exposes pool health and the vdev tree but not per-dataset usage, so
    per-project numbers can only come from `zfs list` on the host itself.
    """
    if not CONFIG.ingest_token:
        raise HTTPException(503, "ingest not configured")
    if authorization.removeprefix("Bearer ").strip() != CONFIG.ingest_token:
        raise HTTPException(401, "bad ingest token")
    body = await request.json()
    rows = body.get("datasets")
    # Two different reporters push here: pve sends ZFS datasets, a desktop sends only
    # its own filesystems. Requiring `datasets` rejected the second with a 400 that
    # said nothing about what was actually wrong.
    if not isinstance(rows, list) and not isinstance(body.get("hosts"), list):
        raise HTTPException(400, "expected {'datasets': [...]} and/or {'hosts': [...]}")
    clean = [r for r in (rows or []) if isinstance(r, dict) and r.get("name")]
    # `dirs` carries the plain directories ZFS cannot break out — /bulk/romfleet
    # is 2.6 TB and is not a dataset. Accepting the field and then not STORING it meant the
    # single largest thing on the estate was pushed every hour and silently dropped.
    dirs = [d for d in (body.get("dirs") or [])
            if isinstance(d, dict) and d.get("path")]
    # Hosts that are not Proxmox nodes -- a desktop reporting its own disks. Merged
    # rather than replaced, so the ZFS pusher on pve and a desktop pusher can update the
    # same snapshot independently without either erasing the other.
    prev = (db.get_snapshot("storage") or (0, {}))[1] or {}
    # Keyed by host AND filesystem: a machine reports more than one. Keying on the host
    # name alone collapsed gaming-pc's root and /boot into a single entry, and the last one
    # in the payload won -- so a 235 GB disk was represented by a 4 GB EFI partition.
    def _key(h):
        return (h.get("host"), h.get("label") or "")
    hosts = {_key(h): h for h in (prev.get("hosts") or [])
             if isinstance(h, dict) and h.get("host")}
    for h in (body.get("hosts") or []):
        if isinstance(h, dict) and h.get("host"):
            hosts[_key(h)] = {**h, "as_of": time.time()}
    # `source` names who reported the DATASETS, and the storage panel labels the pools
    # with it. A desktop pushing only its own filesystems must not claim them: gaming-pc's
    # hosts-only push relabelled pve's Scratch pool as "gaming-pc · Scratch".
    db.put_snapshot("storage", {"datasets": clean or (prev.get("datasets") or []),
                                "dirs": dirs or (prev.get("dirs") or []),
                                "hosts": list(hosts.values()),
                                "source": (body.get("source", "pve") if clean
                                           else prev.get("source", "pve"))})
    db.log_event("ingest.storage", f"{len(clean)} datasets, {len(dirs)} dirs")
    log.info("storage report ingested: %d datasets, %d dirs", len(clean), len(dirs))
    return JSONResponse({"ok": True, "datasets": len(clean), "dirs": len(dirs)})


@app.post("/api/ingest/health")
async def ingest_health(request: Request, authorization: str = Header(default="")):
    """Accept a service-health report: does the app WORK, not what version is it.

    Added 2026-09-12 after OnlyOffice document editing sat broken for 13 days. Sagamore
    knew Nextcloud's version and that its container was up; neither says whether a
    document can be opened. The checks are the service's own diagnostics (`occ
    setupchecks`, `occ onlyoffice:documentserver --check`), run by pve — not by the
    service itself, which cannot be trusted to report its own failure, and not by Home
    Assistant, which would have to hold the app's admin credentials to ask.

    Reports MERGE by service name, each keeping its own timestamp, so one pusher going
    quiet degrades only its own service to unknown instead of erasing the others.
    """
    if not CONFIG.ingest_token:
        raise HTTPException(503, "ingest not configured")
    if authorization.removeprefix("Bearer ").strip() != CONFIG.ingest_token:
        raise HTTPException(401, "bad ingest token")
    body = await request.json()
    service = str(body.get("service") or "").strip()
    checks = body.get("checks")
    if not service or not isinstance(checks, list):
        raise HTTPException(400, "expected {'service': str, 'checks': [...]}")
    clean = [c for c in checks if isinstance(c, dict) and c.get("key") and c.get("state")]
    if not clean:
        raise HTTPException(400, "no usable checks in the payload")
    prev = (db.get_snapshot("health") or (0, {}))[1] or {}
    services = dict(prev.get("services") or {})
    services[service] = {"checks": clean, "as_of": time.time(),
                         "source": body.get("source", "pve")}
    db.put_snapshot("health", {"services": services})
    db.log_event("ingest.health", f"{service}: {len(clean)} checks")
    log.info("health report ingested: %s, %d checks", service, len(clean))
    return JSONResponse({"ok": True, "service": service, "checks": len(clean)})


@app.post("/api/ingest/apps")
async def ingest_apps(request: Request, authorization: str = Header(default="")):
    """Accept the self-hosted-app version report pushed by pve.

    Same push rationale as /api/ingest/patch, plus one of its own: working out
    whether Sonarr or Immich has an update means holding each app's API key.
    Sagamore deliberately holds none of them — pve's app-update.sh already does
    this every morning for the Discord digest, so it reports its findings in
    rather than us duplicating the credentials.

    Entries are pre-classified by the pusher (state: ok|update|failed|unknown)
    so this stays a renderer and the digest's text format stays pve's problem.
    """
    if not CONFIG.ingest_token:
        raise HTTPException(503, "ingest not configured")
    if authorization.removeprefix("Bearer ").strip() != CONFIG.ingest_token:
        raise HTTPException(401, "bad ingest token")
    body = await request.json()
    entries = body.get("apps")
    if not isinstance(entries, list):
        raise HTTPException(400, "expected {'apps': [...]}")
    clean = [a for a in entries if isinstance(a, dict) and a.get("name")]
    prev = (db.get_snapshot("apps") or (0, {}))[1] or {}
    carry_app_first_seen(prev.get("apps"), clean, time.time())
    db.put_snapshot("apps", {"apps": clean, "source": body.get("source", "pve"),
                             "routine": body.get("routine")})
    db.log_event("ingest.apps", f"{len(clean)} apps")
    log.info("app report ingested: %d apps", len(clean))
    return JSONResponse({"ok": True, "apps": len(clean)})


# ---------------------------------------------------------------------------
# Ingest — ES-DE pushes what it just launched
# ---------------------------------------------------------------------------
# Push, not poll, and for a harder reason than the pve reports: NOTHING outside
# ES-DE knows a libretro core just started. There is no API to ask and no process
# name that reliably maps to a game -- the same RetroArch binary runs every system,
# and the emulator for PS2 is a different AppImage again. ES-DE does know, and it
# will run a script on `game-start`, so it reports in.
#
# Box art arrives WITH the push instead of being fetched later: it lives on an autofs
# mount on the gaming PC (MediaDirectory, /mnt/frank-media/<system>/covers/) that
# Sagamore cannot see and should not be given credentials for.
ESDE_ART_MAX = 4 * 1024 * 1024


@app.post("/api/ingest/esde")
async def ingest_esde(request: Request, authorization: str = Header(default="")):
    """Accept a game-start / game-end push from ES-DE on the gaming PC."""
    if not CONFIG.ingest_token:
        raise HTTPException(503, "ingest not configured")
    if authorization.removeprefix("Bearer ").strip() != CONFIG.ingest_token:
        raise HTTPException(401, "bad ingest token")
    body = await request.json()
    state = str(body.get("state") or "").strip().lower()
    if state not in ("playing", "stopped"):
        raise HTTPException(400, "state must be 'playing' or 'stopped'")

    art = body.get("art") or ""
    if state != "playing":
        # A stop carries no game and no art; storing the snapshot rather than
        # deleting it keeps the timestamp, which is how we tell "idle" apart
        # from "this feed has never reported".
        db.put_snapshot("esde", {"state": "stopped"})
        db.log_event("ingest.esde", "stopped")
        return JSONResponse({"ok": True, "state": "stopped"})

    game = str(body.get("game") or "").strip()
    if not game:
        raise HTTPException(400, "playing requires a game name")
    # ⚠️ Bound the art BEFORE storing it. This row goes into SQLite and is read on
    # every page build; an unbounded base64 blob from a mis-scraped 20 MB PNG would
    # bloat the snapshot table and every render with it. Oversized art is dropped,
    # not rejected -- the game name is the point, the picture is a bonus.
    if art and len(art) > ESDE_ART_MAX:
        log.warning("esde: art dropped, %d bytes over the %d cap", len(art), ESDE_ART_MAX)
        art = ""
    db.put_snapshot("esde", {
        "state": "playing",
        "game": game,
        "system": str(body.get("system") or "").strip(),
        "system_full": str(body.get("system_full") or "").strip(),
        "art": art,
        "art_type": str(body.get("art_type") or "image/png").strip(),
    })
    db.log_event("ingest.esde", game)
    log.info("esde: playing %s", game)
    return JSONResponse({"ok": True, "game": game, "art": bool(art)})


@app.get("/api/esde/art")
async def esde_art():
    """The current game's box art, served from our own origin.

    Kept out of the HTML on purpose: the page re-renders on every poll, and a few
    hundred KB of base64 inlined each time is a real cost for a picture that changes
    only when the game does.
    """
    snap = db.get_snapshot("esde")
    if not snap:
        raise HTTPException(404, "no esde snapshot")
    ts, e = snap
    if not (e or {}).get("art"):
        raise HTTPException(404, "no art for the current game")
    try:
        raw = base64.b64decode(e["art"], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(500, "stored art is not valid base64")
    return Response(raw, media_type=e.get("art_type") or "image/png",
                    headers={"Cache-Control": "public, max-age=300",
                             "ETag": f'"esde-{ts}"'})


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Favicons
# ---------------------------------------------------------------------------
# Served from OUR origin, never a third-party favicon CDN. Two reasons: the
# dashboard has to render when the house has no internet (a CDN would leave
# every row blank), and shipping the list of everything you bookmark to Google
# is not a trade worth making for 16px of icon.
#
# Fetched once, cached on disk, and negative results are cached too — otherwise
# every page load retries every dead host and the grid stalls behind timeouts.
_FAVICON_DIR = Path(CONFIG.db_path).parent / "favicons"
_FALLBACK = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16">'
    b'<circle cx="8" cy="8" r="7" fill="none" stroke="#8fa0ab" stroke-width="1.5"/>'
    b'<path d="M1 8h14M8 1a11 11 0 0 1 0 14A11 11 0 0 1 8 1" fill="none" '
    b'stroke="#8fa0ab" stroke-width="1.2"/></svg>'
)
_HOST_OK = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


_LINK_ICON = re.compile(
    r'<link[^>]+rel=["\'][^"\']*icon[^"\']*["\'][^>]*>', re.I)
# Match to the SAME quote that opened the attribute. A [^"']+ class stopped at the first
# quote of EITHER kind, so an inline data: URI whose SVG uses single quotes internally was
# truncated to `data:image/svg+xml,<svg xmlns=` — 11 bytes of nothing, served as an icon.
_HREF = re.compile(r'href=(["\'])(.*?)\1', re.I | re.S)


async def _fetch_icon(host: str) -> bytes | None:
    """Find a host's favicon.

    /favicon.ico alone is not enough: every self-hosted app here 404s on it and
    declares the real icon with <link rel="icon">, sometimes as an inline data:
    URI. Checking only the well-known path made every row fall back to the globe.
    """
    import base64
    from urllib.parse import urljoin, unquote
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True, verify=False,
                                     headers={"User-Agent": "Sagamore"}) as c:
            for scheme in ("https", "http"):
                base = f"{scheme}://{host}"
                try:
                    r = await c.get(f"{base}/favicon.ico")
                    if r.status_code == 200 and r.content and len(r.content) < 200_000 \
                       and not r.headers.get("content-type", "").startswith(
                           ("text/html", "application/json")):
                        return r.content
                except Exception:  # noqa: BLE001
                    pass
                try:
                    page = await c.get(base + "/")
                    if page.status_code != 200:
                        continue
                    m = _LINK_ICON.search(page.text[:60_000])
                    if not m:
                        continue
                    href = _HREF.search(m.group(0))
                    if not href:
                        continue
                    ref = href.group(2).strip()
                    if ref.startswith("data:"):
                        head, _, payload = ref.partition(",")
                        raw = (base64.b64decode(payload) if ";base64" in head
                               else unquote(payload).encode())
                        # 20 bytes is below any real icon; anything smaller means the URI
                        # was truncated. Better the honest globe than a broken image.
                        return raw if 20 < len(raw) < 200_000 else None
                    ic = await c.get(urljoin(base + "/", ref))
                    if ic.status_code == 200 and ic.content and len(ic.content) < 200_000:
                        return ic.content
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        log.debug("favicon discovery failed for %s: %s", host, exc)
    return None


@app.get("/favicon/{host}")
async def favicon(host: str):
    """Cached favicon for a bookmarked host. Always 200 — a missing icon must not
    render as a broken image, so the fallback globe is returned instead."""
    if not _HOST_OK.match(host):
        return Response(content=_FALLBACK, media_type="image/svg+xml")
    _FAVICON_DIR.mkdir(parents=True, exist_ok=True)
    hit = _FAVICON_DIR / f"{host}.bin"
    miss = _FAVICON_DIR / f"{host}.miss"
    if hit.exists():
        return Response(content=hit.read_bytes(),
                        media_type=_guess_type(hit.read_bytes()),
                        headers={"Cache-Control": "public, max-age=604800"})
    if miss.exists() and (time.time() - miss.stat().st_mtime) < 86400:
        return Response(content=_FALLBACK, media_type="image/svg+xml")
    data = await _fetch_icon(host)
    if data:
        hit.write_bytes(data)
        return Response(content=data, media_type=_guess_type(data),
                        headers={"Cache-Control": "public, max-age=604800"})
    miss.touch()
    return Response(content=_FALLBACK, media_type="image/svg+xml")


def _guess_type(b: bytes) -> str:
    if b[:8].startswith(b"\x89PNG"):
        return "image/png"
    if b[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if b[:4] == b"RIFF":
        return "image/webp"
    head = b.lstrip()[:200].lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml") or b"<svg" in head:
        return "image/svg+xml"
    return "image/x-icon"


@app.api_route("/bookmarks/add", methods=["GET", "POST"])
async def add_bookmark(request: Request):
    """Quick-add. Accepts JSON, a form post, or a GET with query params.

    GET is accepted for two reasons. An iOS Shortcut can hit it — and, less
    obviously, **Authentik's forward-auth turns a POST into a GET** when the
    session needs refreshing: the browser is bounced to the outpost and reissues
    the original URL, losing both the method and the form body. Answering 405
    there dead-ends the user on an error page for what is really just an expired
    login, so an empty GET quietly returns them to the dashboard instead.
    """
    ctype = request.headers.get("content-type", "")
    if request.method == "POST":
        data = await request.json() if "json" in ctype else dict(await request.form())
    else:
        data = dict(request.query_params)
    url = (data.get("url") or "").strip()
    if not url:
        # Bounced back from a login redirect with the form body gone.
        return RedirectResponse("/#bookmarks", status_code=303)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    title = (data.get("title") or url).strip()
    folder = (data.get("folder") or "").strip()
    tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
    # Write through to the store, not just locally. Otherwise an added bookmark
    # never reaches a browser, and the next sync — which only replaces rows from
    # the "nextcloud" source — leaves it stranded as a local-only oddity.
    saved_upstream = False
    if nextcloud.configured:
        try:
            await nextcloud.save(url, title, tags, folder or None)
            saved_upstream = True
        except Exception as exc:  # noqa: BLE001
            log.warning("Nextcloud save failed, keeping locally: %s", exc)
    db.upsert_bookmark(url, title, folder or "Unsorted",
                       (data.get("notes") or "").strip(),
                       "nextcloud" if saved_upstream else "manual", ",".join(tags))
    db.log_event("bookmark.add", f"{'nextcloud' if saved_upstream else 'local'}: {url[:120]}")
    if "json" in ctype:
        return JSONResponse({"ok": True, "saved_upstream": saved_upstream})
    return RedirectResponse("/#bookmarks", status_code=303)


@app.post("/bookmarks/import")
async def import_bookmarks(file: UploadFile):
    """Import a browser bookmarks export (Safari/Chrome/Firefox/Edge)."""
    raw = (await file.read()).decode("utf-8", errors="replace")
    marks = parse_netscape_bookmarks(raw)
    db.replace_source("import")
    for m in marks:
        db.upsert_bookmark(m["url"], m["title"], m["folder"], "", "import")
    db.log_event("bookmark.import", f"{len(marks)} from {file.filename}")
    return RedirectResponse("/#bookmarks", status_code=303)


@app.api_route("/bookmarks/capture", methods=["GET", "POST"], response_class=HTMLResponse)
async def capture(request: Request):
    """Save the current page — the de-clouded capture path.

    Everything stays on the LAN/tailnet: a bookmarklet or an iOS Shortcut hits
    this endpoint, and Sagamore forwards to Nextcloud using the credentials it already
    holds. Nothing touches iCloud, and no secret ends up pasted into a bookmark
    or stored on a phone.

    GET is accepted deliberately. A bookmarklet cannot POST cross-origin without
    CORS, but it can open a URL — which is how every hosted read-later service
    has done this since Delicious. The trade-off is a mutation behind a GET;
    acceptable because this endpoint is LAN/tailnet-only and additive.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        ctype = request.headers.get("content-type", "")
        body = await request.json() if "json" in ctype else dict(await request.form())
        params.update({k: v for k, v in body.items() if v})

    url = (params.get("url") or "").strip()
    title = (params.get("title") or "").strip()
    tags = [t for t in (params.get("tags") or "inbox").replace(",", " ").split() if t]

    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")

    saved_to = "local"
    if nextcloud.configured:
        try:
            await nextcloud.save(url, title, tags)
            saved_to = "nextcloud"
        except Exception as exc:  # noqa: BLE001
            log.warning("Nextcloud save failed, keeping locally: %s", exc)
    # Always keep a local copy too, so a Nextcloud outage never loses a capture.
    db.upsert_bookmark(url, title or url, tags[0] if tags else "Inbox", "", "capture")
    db.log_event("bookmark.capture", f"{saved_to}: {url[:120]}")

    if "json" in request.headers.get("accept", "") or request.method == "POST":
        return JSONResponse({"ok": True, "saved_to": saved_to, "url": url})
    # Small self-closing page, so the bookmarklet's popup gets out of the way.
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>Saved</title>"
        "<style>body{font:15px/1.5 -apple-system,sans-serif;padding:1.5rem;"
        "background:#151b1f;color:#e5eaee}b{color:#4cb782}</style>"
        f"<p><b>Saved to Sagamore</b></p><p style='color:#8fa0ab'>{title or url}</p>"
        "<script>setTimeout(()=>window.close(),1200)</script>"
    )


@app.post("/bookmarks/{bid}/delete")
async def del_bookmark(bid: int):
    db.delete_bookmark(bid)
    return RedirectResponse("/#bookmarks", status_code=303)
