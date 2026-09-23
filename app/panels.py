"""Panel builders — raw source data in, judged view models out.

Each builder is a pure function of the collected data so it can be unit-tested
without touching the network, and so a source outage produces an honest UNKNOWN
panel rather than a misleading green one.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import html
import os
from urllib.parse import urlparse
import logging
import re
import unicodedata
import time

from .config import Config
from .model import Panel, Reading, Status, humanise_age, worst
from .liveness import CLASS_MAX_AGE, build_last_seen, device_key, name_stem
from .sources import num, parse_ha_time

# Panels are mostly pure, but a few facts arrive from feeds that can fail in ways
# worth naming rather than rendering as silence. Same logger as the app.
log = logging.getLogger("sagamore")
from .trash_schedule import build_payload

# A battery room sensor that reports every 2-3h is healthy; the 2h thermostat tolerance
# flags it constantly. Six hours still catches a genuinely dead sensor within a quarter day.
BATTERY_CLIMATE_MAX_AGE = 6 * 60 * 60

# A minisplit in standby reads 0-2 W; a running compressor is hundreds. Anything under
# this is idle. Deliberately generous — a false "idle" is better than a false "cooling".
# Positive-evidence threshold only. A minisplit compressor draws hundreds of watts, so
# anything at or above this is unambiguously running. There is deliberately NO idle
# threshold: see the asymmetry note in hvac_panel.
HVAC_RUNNING_WATTS = 100

# Devices that are legitimately unreachable and should not be reported as faults.
# Sonos Roam is battery-powered and lives in a golf bag; the August lock is dead and
# awaiting replacement. Both are decisions about THIS house, so they live in config.
# Devices that are unreachable BY DESIGN rather than faulty. Set HA_IGNORE_DEVICES in the
# environment (comma-separated, exact HA device names) — these are facts about a particular
# house, so the deployment owns the list and the default is empty.
HA_IGNORE_DEVICES = {
    d.strip() for d in os.environ.get("HA_IGNORE_DEVICES", "").split(",") if d.strip()
}


# Quick links, shown as a strip of tiles above the cards. Definitions live here rather
# than in the template because which apps matter is a fact about this house, and the
# icons come from the same public CDN the Discord avatars use -- nothing on this estate
# is reachable from outside, but a browser on the LAN has internet.
#
# Override with FAVORITES in the environment: "Name|url|icon-slug" entries, comma
# separated. Icon slugs are from homarr-labs/dashboard-icons.
_FAVOURITE_DEFAULT = [
    ("Proxmox", "https://proxmox.example.com", "proxmox"),
    ("UniFi", "https://192.168.1.1", "unifi"),
    ("NextDNS", "https://my.nextdns.io", "nextdns"),
    ("Home Assistant", "https://ha.example.com", "home-assistant"),
    ("Plex", "https://plex.example.com", "plex"),
    ("GameVault", "https://gamevault.example.com", "gamevault"),
    ("Sonarr", "https://sonarr.example.com", "sonarr"),
    ("Radarr", "https://radarr.example.com", "radarr"),
    ("Paperless", "https://docs.example.com", "paperless-ngx"),
    ("Immich", "https://photos.example.com", "immich"),
]
ICON_CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons@main/png"


# Hostname -> dashboard-icons slug, so a tagged bookmark still gets a real logo instead
# of a 16px favicon. Anything unmapped falls back to Sagamore's own /favicon/<host>
# endpoint, which already fetches and caches favicons for the bookmark list.
_ICON_BY_HOST = {
    # Internal services sit behind Authentik, so a favicon fetch gets the login
    # page and returns nothing — they render blank without an explicit mapping.
    # Home-grown apps: no CDN logo exists. GameLog and Property Records use their
    # OWN favicons, lifted verbatim from their HTML heads, so the dashboard shows
    # what the browser tab does. IoT-Net has no icon of its own — its login page ships
    # none — so the clock is our choice, matching the emoji convention the other
    # two already set. RetroAchievements has no dashboard-icons slug (it 404s), but
    # GameLog already carries the real logo, so we reuse that rather than invent.
    "games.example.com": "/static/icons/gamelog.svg",
    "realestate.example.com": "/static/icons/realestate.svg",
    "time.example.com": "/static/icons/clock.svg",
    "retroachievements.org": "/static/icons/retroachievements.webp",
    "jd.example.com": "jdownloader",
    "prowlarr.example.com": "prowlarr",
    "sabnzbd.example.com": "sabnzbd",
    "z2m.example.com": "zigbee2mqtt",
    "npm.example.com": "nginx-proxy-manager",
    "apollo.example.com": "kiwix",
    "auth.example.com": "authentik",
    "metube.example.com": "metube",
    "home.example.com": "homarr",
    "login.tailscale.com": "tailscale",
    # Both garage-door openers run ESPHome firmware (the second via ratgdo).
    "192.168.6.75": "esphome", "192.168.6.76": "esphome",
    "play.sonos.com": "sonos",
    "proxmox.example.com": "proxmox", "pve1.example.com": "proxmox",
    "pve3.example.com": "proxmox", "pve4.example.com": "proxmox",
    "pbs.example.com": "proxmox",
    "ha.example.com": "home-assistant",
    "plex.example.com": "plex",
    "gamevault.example.com": "gamevault",
    "sonarr.example.com": "sonarr",
    "radarr.example.com": "radarr",
    "docs.example.com": "paperless-ngx",
    "photos.example.com": "immich",
    "roms.example.com": "romm",
    "abs.example.com": "audiobookshelf",
    "sagamore.example.com": "homarr",
    "my.nextdns.io": "nextdns",
    "vault.example.com": "infisical",
    "nextcloud.example.com": "nextcloud",
    "unifi.ui.com": "unifi",
    "192.168.1.1": "unifi",
    "dash.cloudflare.com": "cloudflare",
}

FAVOURITE_TAG = os.getenv("FAVORITE_TAG", "favorite").strip().lower()


def _icon_for(url: str) -> str:
    host = urlparse(url).hostname or ""
    slug = _ICON_BY_HOST.get(host)
    if slug:
        # A value starting with "/" is a file we ship ourselves, for the
        # home-grown apps that have no logo on the icon CDN.
        if slug.startswith("/"):
            return slug
        return f"{ICON_CDN}/{slug}.png"
    # Sagamore already proxies and caches favicons for the bookmark list.
    return f"/favicon/{host}" if host else ""


def favourites_from_bookmarks(rows: list[dict]) -> list[dict]:
    """Bookmarks carrying the favourite tag, in the order Nextcloud returns them.

    Tags are stored comma-joined and lower-cased, so this is an exact tag test rather
    than a substring one -- "favorite" must not be matched by "favorites-old".
    """
    out = []
    for b in rows or []:
        tags = [t for t in (b.get("tags") or "").split(",") if t]
        if FAVOURITE_TAG in tags:
            out.append({"name": b.get("title") or b.get("url", ""),
                        "url": b.get("url", ""),
                        "icon": _icon_for(b.get("url", ""))})
    return out


def favourites(rows: list[dict] | None = None) -> list[dict]:
    """Tagged bookmarks if there are any, otherwise the built-in list.

    The fallback matters: an empty tag would otherwise wipe the strip off the page with
    no clue why, and the tag lives in Nextcloud where a typo is easy and invisible here.
    """
    tagged = favourites_from_bookmarks(rows or [])
    if tagged:
        return tagged
    raw = os.getenv("FAVORITES", "").strip()
    if raw:
        items = []
        for entry in raw.split(","):
            parts = [x.strip() for x in entry.split("|")]
            if len(parts) == 3 and all(parts):
                items.append(tuple(parts))
        if items:
            return [{"name": n, "url": u, "icon": f"{ICON_CDN}/{i}.png"} for n, u, i in items]
    return [{"name": n, "url": u, "icon": f"{ICON_CDN}/{i}.png"}
            for n, u, i in _FAVOURITE_DEFAULT]


def _ignored_device(name: str) -> bool:
    """Is this device unreachable by design?

    Entries may be exact names or shell-style patterns. Patterns matter for integrations
    that mint one device per ephemeral client: Plex creates a media_player for every
    client that connects and marks it unavailable the moment it disconnects, so an exact
    list would need a new entry for each Apple TV, phone and browser, and would go stale
    silently. "Plex (*" covers the class.
    """
    return any(fnmatch.fnmatch(name, pat) if any(c in pat for c in "*?[")
               else name == pat
               for pat in HA_IGNORE_DEVICES)

# Two integrations watching the same playback report positions a fraction of a second
# apart. 60s is loose enough to survive poll skew between them and tight enough that two
# genuinely different programmes of identical length are not mistaken for one stream.
# A crash or power cut never sends ES-DE's `game-end`, so the pushed "playing" state is
# trusted for six hours and no longer. Long enough for a real session, short enough that
# a dead machine stops claiming to be mid-game the same day.
ESDE_MAX_AGE = 6 * 60 * 60

STREAM_MATCH_SECONDS = 60


def _secs(v: object) -> float | None:
    """A media_duration/position attribute as a number, or None. These arrive as bare
    ints from Home Assistant, not as state dicts, so `num()` is the wrong tool."""
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# Opower polls daily, so `sensor.national_grid_last_updated` advances every day even
# while the billed numbers sit at zero early in a cycle. Two missed polls means the pull
# has broken rather than the cycle being young -- the distinction between an actionless
# monthly gap and something worth looking at.
NG_FEED_MAX_AGE = 48 * 60 * 60

# GameLog exports hourly. Six hours means several missed runs, not one slow one.
GAMING_MAX_AGE = 6 * 60 * 60

# Wifi health thresholds. Satisfaction is UniFi's client-experience score (100 = ideal);
# interference is airtime in use by something that is not us. Both are judged rather than
# raw airtime -- see network_panel for why that distinction matters.
SAT_WARN, SAT_ALERT = 85, 70
NOISE_WARN, NOISE_ALERT = 25, 40

# How stale a reading may get before we stop believing it. These are the
# numbers that turn a value into a judgement — see model.py.
MAX_AGE = {
    "power": CLASS_MAX_AGE["plug"],
    "leak": CLASS_MAX_AGE["leak"],
    "climate": CLASS_MAX_AGE["climate"],
    "smoke": CLASS_MAX_AGE["smoke"],
    "door": None,            # a door legitimately sits unchanged for days
}


# --- Per-circuit trend: "is this using more than usual?" -------------------
# A whole-house "20% over a typical month" tells you the bill is up but not
# which thing to go turn off. These constants turn the stored series into a
# per-circuit answer, and — just as importantly — decide when to keep quiet.
RECENT_WINDOW = 24 * 3600        # "now" = the last day, not this instant
BASELINE_WINDOW = 14 * 24 * 3600  # "usual" = up to a fortnight before that
MIN_BASELINE_SPAN = 6 * 3600     # refuse to call anything usual on less than this
MIN_SAMPLES = 12                 # ...and not on a handful of points either
# Both gates must trip before a circuit is called out. Percent alone is a liar:
# a 3 W phone charger going to 6 W is +100% and means nothing, while 40 W more
# on an always-on load is real money. Watts alone is a liar in the other
# direction: +30 W on a dryer is noise.
TREND_MIN_PCT = 15.0
TREND_MIN_WATTS = 5.0


def circuit_trend(db, entity_id: str, now: float | None = None) -> dict:
    """Compare a circuit's last 24h against its own prior fortnight.

    Returns a dict whose `state` is one of:
      learning  - not enough history yet to have a "usual" (the honest default)
      up / down - a real, material change
      flat      - measured, and unremarkable

    Deliberately compares a circuit only against ITSELF. Cross-circuit
    comparison would be meaningless, and an absolute watt threshold applied to
    a house full of loads spanning three orders of magnitude would either
    scream about the dryer or never mention the desk.
    """
    out = {"state": "learning", "pct": None, "recent": None, "baseline": None}
    if db is None:
        return out
    now = int(now or time.time())
    key = f"power:{entity_id}"
    recent = db.window_stats(key, now - RECENT_WINDOW, now + 1)
    base = db.window_stats(key, now - RECENT_WINDOW - BASELINE_WINDOW, now - RECENT_WINDOW)
    if not recent or not base:
        return out
    if base["span"] < MIN_BASELINE_SPAN or base["n"] < MIN_SAMPLES:
        return out
    if recent["n"] < MIN_SAMPLES:
        return out

    out["recent"], out["baseline"] = recent["avg"], base["avg"]
    out["base_hours"] = base["span"] / 3600.0
    delta = recent["avg"] - base["avg"]
    # A percentage is only meaningful when the thing you are dividing by is
    # itself meaningful. A dishwasher idling at 1.1 W and then running produces
    # "+4193%", which is arithmetically correct and tells you nothing except
    # that it was off before. Below the watt gate the device was effectively
    # idle, so state the change in watts and let the reader draw the obvious
    # conclusion.
    out["pct"] = (delta / base["avg"] * 100) if base["avg"] >= TREND_MIN_WATTS else None
    out["delta_w"] = delta

    big_enough = abs(delta) >= TREND_MIN_WATTS
    pct_enough = out["pct"] is None or abs(out["pct"]) >= TREND_MIN_PCT
    if big_enough and pct_enough:
        out["state"] = "up" if delta > 0 else "down"
    else:
        out["state"] = "flat"
    return out


_MEASURE_WORDS = {"power", "energy", "voltage", "current", "moisture", "temperature",
                  "humidity", "battery", "status", "life", "active"}


def _clean(label: str) -> str:
    """Tidy a human label: drop a trailing measurement word, de-duplicate runs.

    Handles both shapes Home Assistant produces:
      "networkRack networkRack power" -> "Networkrack"
      "the second Garage Door"           -> "the second Garage Door"  (untouched)
    """
    words = re.split(r"[\s_]+", label.strip())
    while words and words[-1].lower() in _MEASURE_WORDS:
        words.pop()
    half = len(words) // 2
    if half and [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
        words = words[:half]
    out, seen = [], set()
    for w in words:
        if w.lower() not in seen:
            out.append(w if any(c.isupper() for c in w[1:]) or "'" in w else w.capitalize())
            seen.add(w.lower())
    return " ".join(out) or label


def _contact_label(eid: str, states: dict | None = None) -> str:
    """A readable name for a contact sensor.

    The integration names them "Dining r Entry Door" -- the suffix is on every one of
    them and carries no information, and it is actively wrong on the windows.
    """
    if eid in SECURITY_DOORS:
        return SECURITY_DOORS[eid]
    name = _friendly(eid, states)
    name = re.sub(r"\s*Entry Door$", "", name, flags=re.I)
    return " ".join(w[:1].upper() + w[1:] for w in name.split())


def _friendly(entity_id: str, states: dict | None = None) -> str:
    """Best available human label, choosing between two imperfect sources.

    Neither source is reliable alone, as this house demonstrates:

      cover.athom_garage_door_00dd68…  id is opaque   friendly_name "Sam's Garage Door"  <- want fn
      sensor.basement_pool_table_power id is perfect  friendly_name "Power"              <- want id
      sensor.gym_light_power           id is perfect  friendly_name "Gym Light gymLight" <- want id

    So: clean both, discard a friendly_name that cleans away to nothing or to a
    bare measurement word, prefer friendly_name when the entity_id carries an
    opaque hex blob or the name has punctuation an id cannot express, and
    otherwise take whichever is shorter — duplication makes a label longer, never
    more informative.
    """
    id_name = _clean(entity_id.split(".", 1)[1])
    fn_raw = ""
    if states:
        fn_raw = str((states.get(entity_id) or {}).get("attributes", {}).get("friendly_name") or "")
    if not fn_raw:
        return id_name

    fn_name = _clean(fn_raw)
    # "Power" as a whole device name is a labelling slip, not a name.
    if not fn_name or fn_name.lower() in _MEASURE_WORDS:
        return id_name
    # An id containing a hex blob (a MAC fragment or device serial) is unreadable.
    if re.search(r"[0-9a-f]{6,}", entity_id) or "'" in fn_name or "’" in fn_name:
        return fn_name
    return fn_name if len(fn_name) <= len(id_name) else id_name


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------
def power_panel(states: dict, cfg: Config, db=None) -> Panel:
    p = Panel(key="power", title="Power")
    if not states:
        p.error = "Home Assistant unreachable"
        return p

    last_seen = build_last_seen(states)
    circuits = []
    for eid, st in states.items():
        attrs = st.get("attributes", {})
        if attrs.get("device_class") != "power" or attrs.get("unit_of_measurement") != "W":
            continue
        watts = num(st)
        energy = num(states.get(eid.replace("_power", "_energy")))
        # Liveness is a property of the DEVICE, not of this one constant channel.
        as_of = last_seen.get(device_key(eid)) or parse_ha_time(st.get("last_updated"))
        circuits.append(
            {
                "entity_id": eid,
                "name": _friendly(eid, states),
                "watts": watts,
                "energy_kwh": energy,
                "as_of": as_of,
                "stale": as_of is not None and (time.time() - as_of) > MAX_AGE["power"],
                "baseline": any(b in eid for b in cfg.baseline_circuits),
                "trend": circuit_trend(db, eid),
            }
        )
    circuits.sort(key=lambda c: (c["watts"] is None, -(c["watts"] or 0)))

    live = [c for c in circuits if c["watts"] is not None and not c["stale"]]
    measured = sum(c["watts"] for c in live)
    p.rows = circuits

    # The point of the per-circuit trend is to answer "what do I go turn off?",
    # so the risers have to be legible with the table still collapsed. Biggest
    # absolute watt increase first - that is the one costing the most, which is
    # not always the one with the largest percentage.
    p.risers = sorted(          # type: ignore[attr-defined]
        [c for c in circuits if c["trend"]["state"] == "up"],
        key=lambda c: -abs(c["trend"].get("delta_w") or 0),
    )
    p.learning = sum(  # type: ignore[attr-defined]
        1 for c in circuits if c["trend"]["state"] == "learning")

    # The utility integration is the only whole-house number available.
    used = num(states.get("sensor.national_grid_current_bill_electric_usage_to_date"))
    forecast = num(states.get("sensor.national_grid_current_bill_electric_forecasted_usage"))
    typical = num(states.get("sensor.national_grid_typical_monthly_electric_usage"))

    # --- How much of the house can we actually see? -----------------------
    # Twelve plugs are a small slice of a house. Showing only measured circuits
    # makes whichever one is largest look like the dominant load, when in truth
    # it may be a rounding error against the unmetered remainder. The utility
    # integration gives the only whole-house figure available, so compare
    # against it and name the gap outright.
    #
    # Caveat, stated on the tile: the utility number is a BILLING-CYCLE AVERAGE
    # refreshed daily, not a live reading. Comparing it to instantaneous watts
    # would be dishonest, so the comparison is average-to-average.
    CYCLE_HOURS = 30.44 * 24
    house_avg = (forecast * 1000 / CYCLE_HOURS) if forecast else None
    unmetered = None
    if house_avg:
        unmetered = max(0.0, house_avg - measured)
        coverage = (measured / house_avg * 100) if house_avg else 0
        p.readings.append(
            Reading("Whole house", round(house_avg), "W", as_of=time.time(),
                    note="cycle average from the utility, refreshed daily — not live")
        )
        p.readings.append(
            Reading("Measured circuits", round(measured), "W", as_of=time.time(),
                    note=f"{len(live)} of {len(circuits)} plugs · {coverage:.0f}% of the house")
        )
        p.readings.append(
            Reading("Unidentified", round(unmetered), "W", as_of=time.time(),
                    status=Status.OK,
                    note="everything not on a metered plug — HVAC, oven, dryer, "
                         "water heater element, lighting")
        )
    else:
        p.readings.append(Reading("Measured circuits", round(measured), "W",
                                  as_of=time.time(),
                                  note=f"{len(live)} of {len(circuits)} reporting"))

    # ⚠️ A BRAND-NEW BILLING CYCLE READS AS ZERO, NOT AS MISSING. Opower resets
    # usage-to-date and the forecast the moment the cycle rolls over, and does not
    # post the new cycle's meter data for a few days. So `forecast == 0` means
    # "the utility has not reported yet", never "the house used nothing".
    #
    # Treating it as a real number produced the headline
    #     "On pace for 0 kWh — 100% under a typical month"
    # on 2026-09-06 (cycle rolled over 09-04). That is exactly the confident
    # falsehood this dashboard exists to avoid, so a non-positive forecast is
    # UNKNOWN and says why. `informational` keeps a routine monthly gap in the
    # utility feed from dragging the whole card amber — the metered circuits
    # below are live and are what the panel's status should reflect.
    # 🚨 BUT "EMPTY" AND "BROKEN" ARE NOT THE SAME THING, and the first version of this
    # fix conflated them: it printed the reassuring "new billing cycle" line and stayed
    # silent whether the utility had simply not posted yet OR the feed had died. An
    # empty forecast at the top of every month is an actionless alert and must stay
    # quiet; a feed that has stopped reporting is actionable and must not.
    #
    # Told apart by whether the integration is still POLLING, which is a different
    # question from whether the utility has DATA:
    #   sensor.national_grid_last_updated  — when opower last successfully polled
    #   sensor.national_grid_last_changed  — when the utility last supplied new data
    # A healthy feed advances `last_updated` daily even while the numbers sit at zero,
    # so a stale one means the pull is broken rather than the cycle being young.
    fc_raw = states.get("sensor.national_grid_current_bill_electric_forecasted_usage")
    fc_state = str((fc_raw or {}).get("state", "")).strip().lower()
    feed_gone = fc_raw is None or fc_state in ("", "unavailable", "unknown", "none")
    feed_ts = parse_ha_time((states.get("sensor.national_grid_last_updated") or {}).get("state"))
    # No poll timestamp at all -> fall back to presence alone rather than guessing;
    # an absent clock must not manufacture a "stalled" verdict.
    feed_stalled = feed_ts is not None and (time.time() - feed_ts) > NG_FEED_MAX_AGE
    feed_broken = feed_gone or feed_stalled
    awaiting_utility = not forecast          # 0 (cycle reset) or absent

    if used is not None:
        p.readings.append(Reading(
            "Billed so far", round(used), "kWh", as_of=time.time(),
            note="new cycle — utility has not posted usage yet"
                 if awaiting_utility and not feed_broken else ""))
    if feed_broken:
        # NOT informational: this one is meant to be seen. The number is unknowable
        # until someone looks at the integration, which is the definition of actionable.
        why = ("the sensor is gone or unavailable" if feed_gone else
               f"no successful poll for over {NG_FEED_MAX_AGE // 3600}h")
        p.readings.append(Reading(
            "Forecast this cycle", None, "kWh", as_of=feed_ts, status=Status.UNKNOWN,
            note=f"National Grid feed is not reporting — {why}. "
                 "Check the opower integration; this is not a new billing cycle."))
    elif awaiting_utility:
        p.readings.append(Reading(
            "Forecast this cycle", None, "kWh", as_of=time.time(),
            status=Status.UNKNOWN, informational=True,
            note="new billing cycle — the utility has not posted usage yet "
                 "(Opower lags a few days). Measured circuits below are live."))
    if forecast and typical:
        delta = (forecast - typical) / typical * 100
        # Being over a "typical" month is a bill, not a fault. Warn, never alert -
        # an August cooling load is legitimately above a yearly average.
        # Being over a typical month is a BILL, not a fault. It was a WARN above +15%,
        # which meant the card sat amber for whole billing cycles at a time and the
        # colour stopped meaning "look at this". The number still says what it says --
        # and now an arrow says which way -- but it no longer claims something is wrong.
        st = Status.OK
        p.readings.append(
            Reading("Forecast this cycle", round(forecast), "kWh", as_of=time.time(),
                    status=st, informational=True,
                    trend="up" if delta > 0 else ("down" if delta < 0 else "flat"),
                    note=f"{delta:+.0f}% vs typical {typical:,.0f}")
        )
        p.headline = (
            f"On pace for {forecast:,.0f} kWh — {abs(delta):.0f}% "
            f"{'over' if delta > 0 else 'under'} a typical month"
        )
    elif circuits:
        p.headline = f"{measured:,.0f} W across {len(live)} measured circuits"

    # Give the template a whole-house denominator so the bar chart is scaled
    # against the real load rather than against the largest measured circuit.
    p.house_avg = house_avg          # type: ignore[attr-defined]
    p.unmetered = unmetered          # type: ignore[attr-defined]

    stale = [c for c in circuits if c["stale"]]
    if stale:
        p.status = worst(p.status, Status.UNKNOWN)
        p.sub = f"{len(stale)} circuit(s) have stopped reporting: " + ", ".join(
            c["name"] for c in stale[:3]
        )
    else:
        top = live[0] if live else None
        if top and house_avg:
            share = top["watts"] / house_avg * 100
            p.sub = (f"Biggest metered draw: {top['name']} at {top['watts']:,.0f} W "
                     f"— only {share:.0f}% of the house")
        elif top:
            p.sub = f"Biggest metered draw: {top['name']} at {top['watts']:,.0f} W"

    # Phantom load: non-baseline circuits idling above a few watts.
    p.phantom = [  # type: ignore[attr-defined]
        c for c in live if not c["baseline"] and 2 <= (c["watts"] or 0) < 25
    ]
    return p


# ---------------------------------------------------------------------------
# Leak watch
# ---------------------------------------------------------------------------
def leak_readings(states: dict, cfg: Config | None = None) -> tuple[list[Reading], str]:
    """Leak sensors, the sump, and the water heater's electrical faults.

    Every row shows last-seen age, because the documented failure here is a
    sensor going quiet while the dashboard keeps showing its last value.

    This was its own card until water became one of the things the Security panel
    covers. It returns readings plus an *urgent* headline -- non-empty only when
    something is actually wrong -- so that folding it into a bigger panel cannot bury
    the highest-consequence alert in the house behind "doors closed, no alarms".
    """
    if not states:
        return [], ""

    class _Bag:
        readings: list = []
    p = _Bag()
    p.readings = []

    last_seen = build_last_seen(states)

    # Is the integration behind these sensors still checking in? A dry
    # SimpliSafe leak sensor publishes nothing for days, so its own silence
    # proves nothing; the integration's polled heartbeat does. Verified
    # 2026-08-21: 30 of 31 SimpliSafe entities were >16h old while the alarm
    # panel had updated 36 seconds earlier — the sensors were fine.
    hb_entity = (cfg.leak_heartbeat if cfg else "alarm_control_panel.alarm_control_panel")
    hb_max = (cfg.leak_heartbeat_max_age if cfg else 3600)
    hb = states.get(hb_entity)
    hb_age = None
    if hb:
        hb_ts = parse_ha_time(hb.get("last_updated"))
        hb_age = (time.time() - hb_ts) if hb_ts else None
    hb_alive = hb_age is not None and hb_age < hb_max

    for eid, st in states.items():
        if not eid.startswith("binary_sensor.") or "moisture" not in eid:
            continue
        wet = st.get("state") == "on"
        own = last_seen.get(device_key(eid)) or parse_ha_time(st.get("last_updated"))
        if hb_alive:
            # Integration is alive -> trust the reported value, and say which
            # evidence we are relying on rather than implying the sensor spoke.
            note = f"quiet, but its hub checked in {int(hb_age // 60)}m ago"
            as_of, max_age = time.time(), None
        else:
            note = ("hub has not checked in — cannot confirm this sensor is live"
                    if hb else "")
            as_of, max_age = own, MAX_AGE["leak"]
        p.readings.append(
            Reading(
                _friendly(eid, states),
                "WET" if wet else "Dry",
                as_of=as_of, max_age=max_age,
                status=Status.ALERT if wet else Status.OK,
                note=("" if wet else note),
            )
        )

    if hb and not hb_alive:
        p.readings.append(
            Reading("Leak sensor hub", "not checking in", as_of=None,
                    status=Status.ALERT,
                    note=f"{hb_entity} last updated "
                         f"{int((hb_age or 0) // 3600)}h ago — leak alerts cannot be trusted")
        )

    # Battery flags on the same sensors — a dead battery is a silent failure.
    for eid, st in states.items():
        if eid.startswith("binary_sensor.") and eid.endswith("_battery") and "leak" in eid:
            low = st.get("state") == "on"
            if low:
                p.readings.append(
                    Reading(_friendly(eid, states), "LOW BATTERY", status=Status.WARN,
                            as_of=parse_ha_time(st.get("last_updated")))
                )

    # The sump is the highest-consequence item in the house.
    volts = states.get("sensor.sump_pump_voltage")
    v = num(volts)
    p.readings.append(
        Reading(
            "Sump pump supply",
            # `if v` would be a bug: 0 V is the MOST important reading this
            # sensor can produce (supply lost), not an absent one.
            f"{v:.0f} V" if v is not None else None,
            # ⚠️ NOT a literal "sump_pump" key. Once liveness groups by Home
            # Assistant's real device id, the key is an opaque id and any
            # hardcoded name silently misses, which would quietly downgrade the
            # highest-consequence reading in the house to its own last_updated.
            as_of=(last_seen.get(device_key("sensor.sump_pump_voltage")) or
                   (parse_ha_time(volts.get("last_updated")) if volts else None)),
            max_age=MAX_AGE["leak"],
            status=Status.OK if v and v > 100 else Status.ALERT,
            note="Loss of supply here is the scenario that floods the basement",
        )
    )

    for eid, label in (
        ("binary_sensor.waterheater_waterheater_overheating", "Water heater overheating"),
        ("binary_sensor.waterheater_waterheater_overcurrent", "Water heater overcurrent"),
    ):
        st = states.get(eid)
        if st:
            bad = st.get("state") == "on"
            if bad:
                p.readings.append(Reading(label, "FAULT", status=Status.ALERT,
                                          as_of=parse_ha_time(st.get("last_updated"))))

    wet = [r for r in p.readings if r.value == "WET"]
    unknown = [r for r in p.readings if r.effective_status is Status.UNKNOWN]
    if wet:
        urgent = "WATER DETECTED — " + ", ".join(r.label for r in wet)
    elif not hb_alive and hb:
        urgent = "Leak coverage unverifiable — the sensor hub is not checking in"
    elif unknown:
        urgent = f"{len(unknown)} water sensor(s) not reporting — coverage is not what it looks like"
    else:
        urgent = ""
    return p.readings, urgent


# ---------------------------------------------------------------------------
# HVAC
# ---------------------------------------------------------------------------
def hvac_panel(states: dict) -> Panel:
    p = Panel(key="hvac", title="HVAC")
    if not states:
        p.error = "Home Assistant unreachable"
        return p

    running, unknown_action, stuck = [], [], []
    for eid, st in states.items():
        if not eid.startswith("climate."):
            continue
        a = st.get("attributes", {})
        # `hvac_action` is what the unit is DOING; `state` is only the mode it is SET to.
        # Falling back to the mode made a minisplit whose integration reports no action at
        # all render as "doing: cool" — while the panel headline, which reads actions
        # properly, said "nothing calling for heat or cool". The table and the headline
        # disagreed from the same data, and the table was the one inventing an answer.
        # An absent action is unknown, and this dashboard renders unknown as unknown.
        action = a.get("hvac_action")
        mode = st.get("state")
        cur = a.get("current_temperature")
        lo, hi, tgt = a.get("target_temp_low"), a.get("target_temp_high"), a.get("temperature")
        unreachable = ""
        # A unit that is OFF is not a blind spot — it is known to be doing nothing. Until
        # 2026-09-12 an off zone fell straight through to `action is None` and was counted
        # as "not reporting heat/cool state", so switching both minisplits off made the
        # card warn that it could not tell what they were doing. Same complaint as
        # 2026-09-09 ("that should be a good thing"), in the one case not covered then.
        if mode == "off":
            action = "off"
        if action is None and mode and mode != "off":
            # The Samsung minisplits expose no `hvac_action`, and SmartThings has no such
            # field either (verified against the app 2026-09-07) -- so the gap is upstream
            # and no amount of HA plumbing will produce one.
            #
            # 🚫 THIS USED TO GUESS FROM THE POWER SENSOR, AND THAT WAS UNSOUND. The claim
            # was "standby is 0-2 W, anything running is orders of magnitude higher". The
            # highest value ever recorded on these units is 50 W -- nowhere near a
            # compressor -- and the sensor only produces ~9 samples in 6 hours, roughly one
            # every 40 minutes, so it cannot see a compressor cycle at all. An instantaneous
            # read from it can neither prove nor disprove "running".
            #
            # Mode, setpoint and room temperature ARE reliable (SmartThings shows the same
            # three), and together they say whether the unit has anything to do. A zone
            # already past its setpoint is SATISFIED -- which is why nothing is running, and
            # a far more useful thing to print than "idle".
            # ⚠️ THE POWER SENSOR IS ASYMMETRIC EVIDENCE, and conflating the two
            # directions is what made this wrong twice. A reading of hundreds of watts
            # CANNOT happen at standby, so high power positively proves the unit is
            # running. A LOW reading proves nothing: the sensor emits ~9 samples in 6
            # hours, so a compressor cycle between samples is invisible. Trust it to
            # say "running", never to say "idle".
            watts = num(states.get(f"sensor.{name_stem(eid)}_power"))
            want = tgt if tgt is not None else (hi if mode == "cool" else lo)
            if watts is not None and watts >= HVAC_RUNNING_WATTS:
                action = {"cool": "cooling", "heat": "heating", "dry": "drying",
                          "fan_only": "fan"}.get(mode, "running")
            elif cur is not None and want is not None:
                if mode == "cool":
                    action = "satisfied" if cur <= want else "should be cooling"
                    # Cool mode cannot RAISE a temperature. A room sitting below its cooling
                    # setpoint will stay there for ever, which looks like a broken unit and
                    # is really a mode mismatch -- exactly the 2026-09-07 confusion, where
                    # both minisplits sat "on, set to 70" in a 66 F room.
                    if cur < want:
                        unreachable = (f"cool mode cannot warm the room — "
                                       f"{cur:.0f}° is already below the {want:.0f}° setpoint")
                elif mode == "heat":
                    action = "satisfied" if cur >= want else "should be heating"
                    if cur > want:
                        unreachable = (f"heat mode cannot cool the room — "
                                       f"{cur:.0f}° is already above the {want:.0f}° setpoint")
                else:
                    action = "on"
            # No setpoint and no convincing power reading: we genuinely cannot tell.
            # Leaving this as None keeps the zone in the blind-spot list rather than
            # inventing a cheerful "on" for something we have no evidence about.
        target = f"{lo:.0f}–{hi:.0f}" if lo and hi else (f"{tgt:.0f}" if tgt else "—")
        if action in ("heating", "cooling", "should be cooling", "should be heating"):
            running.append(f"{_friendly(eid, states)} {action}")
        if unreachable:
            stuck.append(f"{_friendly(eid, states)}: {unreachable}")
        # A zone that will not say what it is doing is a blind spot, not an idle zone — so
        # it must not be silently absorbed into "nothing calling". NB this is about HVAC
        # activity (is the unit running), nothing to do with occupancy or motion.
        elif action is None:
            unknown_action.append(_friendly(eid, states))
        p.rows.append(
            {
                "name": _friendly(eid, states),
                "mode": st.get("state"),
                "action": action or "unknown",
                "current": cur,
                "target": target,
                "as_of": parse_ha_time(st.get("last_updated")),
            }
        )

    # Room temperatures that aren't attached to a thermostat.
    #
    # Liveness is per DEVICE, not per reading — the same rule the power and leak panels
    # already follow, and this panel was the one place still using a bare last_updated.
    # A room that holds 67° publishes no new state, so its own timestamp ages while the
    # device is demonstrably alive: primary_en_suite's temperature read 11.6h stale while
    # its thermostat entity had updated seconds earlier, and the card said NOT REPORTING
    # about a device that was reporting fine.
    last_seen = build_last_seen(states)

    # Battery sensors report on a slow interval by design. Robin's room sensor publishes
    # roughly every 2-3h — against the 2h thermostat tolerance that is a permanent false
    # alarm, so a battery-powered device gets a longer leash. Presence of a battery entity
    # on the same device is the signal; nothing else distinguishes them.
    battery_devices = {device_key(e) for e in states
                       if e.startswith("sensor.") and e.endswith("_battery")}
    temps: list[Reading] = []

    # Every room with a temperature reading is listed here, zones included — the duplication
    # that made this card unreadable was the same number appearing as a reading AND in the
    # table's "Now" column, so the TABLE dropped that column instead. The table now carries
    # only what a room's temperature cannot tell you: mode, what it is doing, and the target.

    for eid, st in states.items():
        if not eid.startswith("sensor."):
            continue
        if st.get("attributes", {}).get("device_class") != "temperature":
            continue
        if any(k in eid for k in ("chip", "setpoint", "tomorrow", "fridge", "freezer",
                                  "outdoor", "forecast")):
            continue
        t = num(st)
        if t is None:
            continue
        key = device_key(eid)
        as_of = last_seen.get(key) or parse_ha_time(st.get("last_updated"))
        tol = BATTERY_CLIMATE_MAX_AGE if key in battery_devices else MAX_AGE["climate"]
        temps.append(
            Reading(_friendly(eid, states), round(t, 1), "°F", as_of=as_of, max_age=tol)
        )

    # Alphabetical. Dict order here is HA's entity order, which is neither meaningful nor
    # stable, so the same card reshuffled itself between refreshes.
    p.readings.extend(sorted(temps, key=lambda r: r.label.lower()))

    # ---- maintenance monitors, grouped after the temperatures ----
    # Minisplit dust filters. Separate from the furnace filter below because these report a
    # percentage of life remaining rather than a date, and they are cleaned, not replaced.
    maint: list[Reading] = []
    for eid, st in sorted(states.items()):
        if not (eid.startswith("sensor.") and eid.endswith("_filter_life")):
            continue
        # The range hood already has its own explicit reading further down (and a charcoal
        # filter nobody asked to see here). Catching every *_filter_life listed it twice.
        if "zve_" in eid or "grease" in eid or "charcoal" in eid:
            continue
        pct = num(st)
        if pct is None:
            continue
        maint.append(
            Reading(_friendly(eid, states), f"{pct:.0f}%", group="maint",
                    as_of=parse_ha_time(st.get("last_updated")),
                    status=Status.OK if pct > 20 else (Status.WARN if pct > 5 else Status.ALERT),
                    note="life remaining — clean the filter")
        )

    p.readings.extend(sorted(maint, key=lambda r: r.label.lower()))

    # Filter / service intervals — these are the things that actually get forgotten.
    today = dt.date.today()
    # `lead` is the days-before-due the reading starts flagging. The furnace filter is a
    # two-minute swap Alex would rather hear about the day it's due than a fortnight early
    # (a standing amber for two weeks just becomes wallpaper); the annual HVAC service keeps
    # its 14-day lead so it can actually be scheduled.
    for eid, label, interval, lead in (
        ("input_datetime.hvac_filter_last_changed", "Furnace filter", 90, 0),
        ("input_datetime.hvac_last_serviced", "HVAC service", 365, 14),
    ):
        st = states.get(eid)
        if not st:
            continue
        try:
            last = dt.date.fromisoformat(str(st["state"])[:10])
        except (ValueError, KeyError):
            continue
        age = (today - last).days
        due_in = interval - age
        status = Status.OK if due_in > lead else (Status.WARN if due_in > 0 else Status.ALERT)
        note = ("due today" if due_in == 0 else
                "overdue" if due_in < 0 else f"due in {due_in}d")
        p.readings.append(
            Reading(label, f"{age}d ago", as_of=time.time(), status=status, group="maint",
                    note=note)
        )

    # The range hood reports real filter life.
    gl = num(states.get("sensor.zve_e36ds_grease_filter_life"))
    if gl is not None:
        p.readings.append(
            Reading("Range hood grease filter", f"{gl:.0f}%", as_of=time.time(), group="maint",
                    status=Status.OK if gl > 25 else (Status.WARN if gl > 5 else Status.ALERT),
                    note="life remaining")
        )

    if running:
        p.headline = ", ".join(running)
    elif unknown_action:
        # "Idle" would be a claim we cannot support: these zones do not report an action,
        # so we do not know whether they are running. Say that instead of guessing quiet.
        #
        # ⚠️ LEAD WITH THE BLIND SPOT. This headline used to open "Nothing known to be
        # running", which reads as reassurance -- "nothing is running, good" -- while the
        # card sits UNKNOWN. The colour and the words disagreed, and the words won. Name
        # the zones, because "which ones" is the only actionable part.
        n = len(unknown_action)
        who = ", ".join(unknown_action[:3]) + ("…" if n > 3 else "")
        p.headline = (f"Can't tell if anything is running — {n} zone"
                      f"{'s' if n != 1 else ''} not reporting heat/cool state: {who}")
        p.status = worst(p.status, Status.UNKNOWN)
    elif stuck:
        # "Idle" alone is technically true here and completely unhelpful: the zones are
        # ON, just set to a mode that can never reach the number on the display. Say both
        # -- nothing is calling, and here is why -- without the alarm.
        n = len(stuck)
        p.headline = (f"Idle — {n} zone{'s' if n != 1 else ''} already past "
                      f"the setpoint in the current mode")
    else:
        p.headline = "Idle — nothing calling for heat or cool"
    # A zone that can never reach its setpoint in its current mode is the one genuinely
    # actionable thing this card can tell you, and it is invisible in every other view:
    # SmartThings and Home Assistant both happily show "Cool, 70°" next to a 66° room.
    # ⚠️ INFORMATIONAL, NOT A WARNING. Nothing is failing here: the units are working
    # exactly as set, and the setting is the reason nothing happens. Colouring the card
    # for a deliberate configuration turns a permanent condition into a permanent alert
    # -- a cool-mode standby left on through a mild week would sit amber for days with
    # nothing to do about it. The distinction this card should colour on is a system
    # failing to do what it is SET to do; this is the opposite of that.
    #
    # The insight still earns its place on the card, just not in the attention banner.
    if stuck:
        p.readings.append(Reading(
            "Setpoint unreachable", f"{len(stuck)} zone{'s' if len(stuck) != 1 else ''}",
            as_of=time.time(), status=Status.OK, informational=True, group="maint",
            note="; ".join(stuck)))
    overdue = [r for r in p.readings if r.status is Status.ALERT]
    if overdue:
        p.sub = "Needs attention: " + ", ".join(r.label for r in overdue)
    elif stuck:
        p.sub = stuck[0]
    return p


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
def security_panel(states: dict, unifi: list | None = None,
                   cfg: Config | None = None) -> Panel:
    """Everything that answers "is the house shut and safe" -- and nothing else.

    Network client counts used to live here and have moved to Network Health. They
    were the reason this card had outgrown itself: "how many things are on IoT" and
    "is the back door open" are not the same question and do not belong in one place.

    Windows are not here because the house has no window contacts: Home Assistant
    reports ten door sensors and zero of device_class `window`. Rather than render an
    empty "Windows" row implying coverage that does not exist, they are simply absent.
    """
    p = Panel(key="security", title="Security")
    if not states:
        p.error = "Home Assistant unreachable"
        return p

    open_things: list[str] = []

    # ---- garage doors ----
    for eid, st in sorted(states.items()):
        if not eid.startswith("cover.") or "garage" not in eid:
            continue
        is_open = st.get("state") == "open"
        if is_open:
            open_things.append(_friendly(eid, states))
        p.readings.append(
            Reading(_friendly(eid, states), st.get("state", "").title(),
                    as_of=parse_ha_time(st.get("last_changed")), group="doors",
                    max_age=MAX_AGE["door"],
                    status=Status.WARN if is_open else Status.OK)
        )

    # ---- entry doors and windows ----
    # Listed individually this is seven rows all saying "Closed". The useful shape is
    # one line each with the exception called out: how many, and which are open.
    #
    # Doors and windows are split by SECURITY_DOORS, not by device_class, because Home
    # Assistant calls all of them "door" -- see that setting for why.
    groups: dict[str, list[str]] = {"door": [], "window": []}
    for eid, st in sorted(states.items()):
        if not eid.startswith("binary_sensor."):
            continue
        if st.get("attributes", {}).get("device_class") != "door":
            continue
        # An appliance door is not a way into the house.
        if any(k in eid for k in ("dryer", "washer", "fridge", "freezer", "oven")):
            continue
        # A LOCK'S OWN DoorSense CONTACT IS NOT AN EXTRA WAY INTO THE HOUSE. It is a
        # second opinion on a door that already has its own contact, and the Yale
        # reports it `unknown` unless DoorSense is calibrated -- which is exactly
        # what turned the whole Security card UNKNOWN on 2026-09-06, a week after
        # the lock went in. Skip any binary_sensor sharing an object_id with a lock
        # (binary_sensor.garage_door_lock <-> lock.garage_door_lock); that door is
        # already covered by binary_sensor.fr_garage_entry_door, and the lock itself
        # is reported in its own row below.
        if "lock." + eid.split(".", 1)[1] in states:
            continue
        # August stack, retired 2026-09 -- its contacts lingered `unknown` the same way.
        if "august" in eid or "garage_door_door" in eid:
            continue
        groups["door" if eid in SECURITY_DOORS else "window"].append(eid)

    # One combined, ALWAYS-VISIBLE summary. "Is anything open?" is the question actually
    # asked on the way to bed, and the per-kind rows below live in the collapsed group --
    # they are promoted when something is open, but say nothing while all is well, so
    # there was no way to confirm the house was shut. This line answers that standing
    # question; the rows below still carry which-and-where.
    dw_all = groups["door"] + groups["window"]
    dw_open = [_contact_label(e, states) for e in dw_all
               if states[e].get("state") == "on"]
    dw_blind = [_contact_label(e, states) for e in dw_all
                if states[e].get("state") in ("unavailable", "unknown")]

    for kind, plural in (("door", "Entry doors"), ("window", "Windows")):
        eids = groups[kind]
        if not eids:
            continue
        open_now = [_contact_label(e, states) for e in eids
                    if states[e].get("state") == "on"]
        blind = [_contact_label(e, states) for e in eids
                 if states[e].get("state") in ("unavailable", "unknown")]
        open_things.extend(open_now)
        p.readings.append(Reading(
            f"{plural} ×{len(eids)}",
            f"{len(open_now)} open" if open_now else "All closed",
            as_of=time.time(), group="doors",
            status=(Status.WARN if open_now else Status.OK),
            note=(", ".join(open_now) if open_now
                  else (f"not reporting: {', '.join(blind)}" if blind
                        # When everything is shut, the useful thing is which openings
                        # are actually covered -- the count alone does not say that.
                        else ", ".join(_contact_label(e, states) for e in eids))),
        ))

    # ---- locks ----
    locks = {e: v for e, v in states.items() if e.startswith("lock.")}
    if locks:
        dead = [e for e, v in locks.items()
                if v.get("state") in ("unavailable", "unknown")]
        locked = [e for e, v in locks.items() if v.get("state") == "locked"]
        # Both August locks are known dead with a replacement ordered -- that is a fact
        # already written down in HA_IGNORE_DEVICES, not a new discovery. Reporting it
        # as a fault every minute would be an alarm nobody can clear, so it is stated
        # once, informationally, and kept out of the roll-up.
        # The August stack was retired in September 2026 and replaced by a single
        # Z-Wave Yale (lock.garage_door_lock). The text here used to assert "both
        # August locks are dead, a replacement is on order" and "not reporting: 0"
        # when healthy -- both were still on the card after the new lock was live
        # and locking. Say what is actually true: name each lock and its state, and
        # only change colour for one that has stopped reporting.
        #
        # An unlocked door in the middle of the day is NOT a fault, so it is named
        # without going amber. That was the previous judgement too; keep it.
        all_dead = len(dead) == len(locks)
        if all_dead:
            summary = "offline"
            note = "no lock has reported recently"
        else:
            summary = f"{len(locked)} of {len(locks)} locked"
            note = ", ".join(
                f"{_friendly(e, states)} {locks[e].get('state') or 'unknown'}"
                for e in sorted(locks)
            )
            if dead:
                note += " — not reporting: " + ", ".join(
                    _friendly(e, states) for e in sorted(dead))
        p.readings.append(Reading(
            f"Door locks ×{len(locks)}", summary,
            as_of=time.time(), group="doors", informational=all_dead,
            status=Status.OK if not dead else Status.WARN,
            note=note,
        ))

    # ---- alarm ----
    for eid, st in sorted(states.items()):
        if not eid.startswith("alarm_control_panel."):
            continue
        state = str(st.get("state") or "")
        p.readings.append(Reading(
            "Alarm system", state.replace("_", " ").title(),
            as_of=parse_ha_time(st.get("last_changed")), group="alarm",
            max_age=MAX_AGE["door"],
            # Disarmed is not a fault -- it is the normal state of an occupied house.
            # Only triggered is.
            status=Status.ALERT if "trigger" in state else Status.OK,
            note=("ALARM TRIGGERED" if "trigger" in state
                  else "SimpliSafe — also the heartbeat the leak sensors are judged by"),
        ))

    # Sits directly under the alarm because it answers the same question at a glance,
    # and shares its group so it is never folded away. ⚠️ A contact that is not
    # reporting must not read as "closed" -- an unseen door is not a shut one -- so a
    # blind sensor makes this UNKNOWN rather than green.
    if dw_all:
        p.readings.append(Reading(
            "Doors & windows",
            f"{len(dw_open)} open" if dw_open else "All closed",
            as_of=time.time(), group="alarm", max_age=MAX_AGE["door"],
            status=(Status.WARN if dw_open
                    else Status.UNKNOWN if dw_blind else Status.OK),
            note=(", ".join(dw_open) if dw_open
                  else (f"not reporting: {', '.join(dw_blind)}" if dw_blind
                        else f"all {len(dw_all)} contacts closed and reporting")),
        ))

    # ---- smoke / CO ----
    hazards = {"smoke": [], "co": [], "heat": [], "battery": []}
    protects: set[str] = set()
    for eid, st in states.items():
        m = re.match(r"binary_sensor\.(.+?)_nest_protect_.*_(smoke|co|heat)_status$", eid)
        b = re.match(r"binary_sensor\.(.+?)_nest_protect_.*_battery_health$", eid)
        if m:
            protects.add(m.group(1))
            if st.get("state") == "on":
                hazards[m.group(2)].append(m.group(1).replace("_", " ").title())
        elif b and st.get("state") == "on":
            hazards["battery"].append(b.group(1).replace("_", " ").title())

    alarms = hazards["smoke"] + hazards["co"] + hazards["heat"]
    if protects:
        p.readings.append(
            Reading(
                f"Nest Protect ×{len(protects)}",
                "ALARM" if alarms else "Clear",
                as_of=time.time(), max_age=MAX_AGE["smoke"], group="hazard",
                status=Status.ALERT if alarms else (Status.WARN if hazards["battery"] else Status.OK),
                note=("; ".join(alarms) if alarms
                      else (f"low battery: {', '.join(hazards['battery'])}" if hazards["battery"]
                            else "smoke, CO and heat all clear")),
            )
        )

    # ---- water ----
    leaks, leak_urgent = leak_readings(states, cfg)
    for r in leaks:
        r.group = "water"
    p.readings.extend(leaks)

    # ---- an unknown device on a network we trust ----
    if unifi is not None:
        trusted = set(cfg.unifi_trusted_networks if cfg else ())
        days = (cfg.unifi_new_device_days if cfg else 7)
        now = time.time()
        # New AND unnamed, not merely unnamed: 13 devices here are permanently unnamed
        # (Proxmox containers, the Xbox, a laptop), so the broader check would sit amber
        # forever and be ignored within a week.
        strangers = [c for c in unifi
                     if c["network"] in trusted and not c["named"]
                     and c["first_seen"] and (now - c["first_seen"]) < days * 86400]
        p.readings.append(Reading(
            "Unrecognised arrivals", len(strangers), as_of=now, group="network",
            status=Status.WARN if strangers else Status.OK,
            note=(", ".join(f"{c['label']} ({c['ip'] or c['mac']})" for c in strangers[:5])
                  if strangers
                  else f"nothing unnamed has joined {', '.join(sorted(trusted))} in {days} days"),
        ))

    # ---- what stays on the face of the card ----
    #
    # Alarm state and smoke/CO are always visible: they are the two you would actually
    # look for. Everything else -- doors, windows, locks, water -- moves into a
    # collapsed list WHILE IT IS FINE, and is promoted back the instant it is not.
    #
    # The promotion is done here rather than in the template on purpose: whether a
    # reading needs attention is a judgement about the house, and a template that had to
    # work it out could get it wrong and hide something. Note `effective_status`, not
    # `status` -- a sensor that has gone quiet is exactly the case that must not be
    # folded away, and only effective_status knows that.
    ALWAYS = ("alarm", "hazard")
    for r in p.readings:
        if r.group in ALWAYS:
            continue
        if r.effective_status is Status.OK:
            r.group = "quiet"

    # Water beats everything: a flooding basement is not a footnote under "doors closed".
    if leak_urgent:
        p.headline = leak_urgent
    elif alarms:
        p.headline = "SMOKE/CO ALARM — " + ", ".join(alarms)
    elif open_things:
        p.headline = f"{', '.join(open_things)} open"
    else:
        p.headline = "House secure — doors closed, no alarms, everything dry"
    return p


# ---------------------------------------------------------------------------
# Network health
# ---------------------------------------------------------------------------
def network_panel(clients: list | None = None, health: dict | None = None,
                  devices: list | None = None, cfg: Config | None = None,
                  ap_satisfaction: dict | None = None) -> Panel:
    """What the network is doing, separate from what the house is doing.

    Client counts live here rather than on the Security card because "32 things on
    IoT" is a capacity fact, not a safety one. The one exception kept on Security is
    an unrecognised device arriving on a trusted network, which genuinely is.
    """
    p = Panel(key="network", title="Network Health")
    if clients is None and not health and not devices:
        p.error = "UniFi controller unreachable"
        return p

    now = time.time()
    health = health or {}

    # ---- internet ----
    www = health.get("www") or {}
    wan = health.get("wan") or {}
    if www or wan:
        up = (wan.get("status") or www.get("status")) == "ok"
        lat = www.get("latency")
        p.readings.append(Reading(
            "Internet", "Up" if up else "Down", as_of=now, group="wan",
            status=Status.OK if up else Status.ALERT,
            note=(f"{lat} ms to the controller's probe" if lat is not None else ""),
        ))
        down, upl = www.get("xput_down"), www.get("xput_up")
        ran = www.get("speedtest_lastrun")
        if down and upl:
            # Stamped with when the speedtest actually ran, not with now. A figure from
            # last Tuesday presented as current is the exact failure this project exists
            # to avoid -- so it ages, and goes unknown when it is stale.
            p.readings.append(Reading(
                "Throughput", f"{down:,.0f} / {upl:,.0f} Mbps", group="wan",
                as_of=ran or None, max_age=48 * 3600,
                note=f"last speedtest · {www.get('speedtest_ping', '?')} ms ping",
            ))

    # ---- controller inventory ----
    if devices:
        offline = [d for d in devices if d.get("state") != 1]
        kinds = {"uap": "AP", "usw": "switch", "udm": "gateway", "ugw": "gateway"}
        counts: dict[str, int] = {}
        for d in devices:
            counts[kinds.get(d.get("type"), d.get("type") or "?")] = \
                counts.get(kinds.get(d.get("type"), d.get("type") or "?"), 0) + 1
        p.readings.append(Reading(
            "UniFi devices", len(devices), as_of=now, group="wan",
            status=Status.ALERT if offline else Status.OK,
            note=(", ".join(f"{d['name']} offline" for d in offline) if offline
                  else " · ".join(f"{n} {k}{'es' if k == 'switch' and n != 1 else 's' if n != 1 else ''}"
                                  for k, n in sorted(counts.items()))),
        ))
        upg = [d for d in devices if d.get("upgradable")]
        if upg:
            p.readings.append(Reading(
                "Firmware updates", len(upg), as_of=now, group="wan", status=Status.WARN,
                note=", ".join(d["name"] for d in upg)))

    # ---- clients per network ----
    if clients:
        guest_nets = set(cfg.unifi_guest_networks if cfg else ("Guest",))
        by_net: dict[str, list[dict]] = {}
        for c in clients:
            by_net.setdefault(c["network"] or "(no network)", []).append(c)
        # Busiest first: a crowded network is the interesting line, not whichever one
        # sorts first alphabetically.
        for net, cs in sorted(by_net.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            wired = sum(1 for c in cs if c["wired"])
            ssids = sorted({c["essid"] for c in cs if c["essid"]})
            bits = []
            if wired:
                bits.append(f"{wired} wired")
            if ssids:
                bits.append("SSID " + ", ".join(ssids))
            is_guest = net in guest_nets
            p.readings.append(Reading(
                net, len(cs), as_of=now, group="net",
                # The guest network should normally be empty, and nobody ever looks at
                # it. A visitor is not a threat; an unnoticed long-term resident is.
                status=Status.WARN if is_guest else Status.OK,
                note=(("visitor network — " + ", ".join(c["label"] for c in cs[:4]))
                      if is_guest else " · ".join(bits)),
            ))

    # ---- per-radio airtime ----
    #
    # Judged on interference and on client experience, NOT on total airtime.
    #
    # Total airtime alone raised a false alarm the first day it existed: the Family Room
    # 5 GHz radio hit 88% and the card went red while every client on it reported a
    # satisfaction of 99 and essentially all of that airtime was our own traffic. A radio
    # busy because someone is pulling a large download is a radio doing its job, and one
    # instantaneous sample from a 15-minute poll is not a trend either.
    #
    # What actually degrades wifi is airtime we cannot control -- a neighbour's AP on the
    # same channel -- because we cannot fix it by doing less. And the honest measure of
    # whether it matters is UniFi's own satisfaction score, which is what using the wifi
    # feels like.
    for d in (devices or []):
        for r in d.get("radios") or []:
            noise = r.get("interference")
            sat = r.get("satisfaction")               # instantaneous -- display only
            # Fall back to the sample only when the rollup has nothing for this AP
            # (a brand-new AP, or the report endpoint failing) -- degraded, not blind.
            judged = (ap_satisfaction or {}).get(d["name"], sat)
            # ⚠️ JUDGE ON THE SUSTAINED FIGURE, NOT THIS SAMPLE. `satisfaction` here is
            # one instantaneous reading, and the radios are read once every 15 minutes.
            # A single sample is not a condition: on 2026-09-08 this radio was caught at
            # 88% airtime and 60% satisfaction, the card went red, and UniFi's own hourly
            # rollup for the same AP never left 95-97 all day. `ap_satisfaction` is that
            # rollup -- every client, whole hours -- so it decides. The instantaneous
            # number stays in the table as detail.
            #
            # ⚠️ SATISFACTION DECIDES; interference only escalates. The comment above
            # already calls satisfaction "the honest measure of whether it matters" --
            # this code did not follow its own reasoning, and warned on interference
            # alone while every client was happy.
            #
            # Measured 2026-09-07: Upstairs Hallway 2.4 GHz sat at 25-33% outside
            # airtime with satisfaction 95, and 11 of its 13 clients between 97 and 100.
            # 2.4 GHz in a dense neighbourhood is congested by definition -- 42 other
            # APs are visible on channel 1 alone -- and with three APs already on the
            # only non-overlapping set (1/6/11) there is no channel to move to. A
            # permanent amber for a neighbour's wifi is the definition of an alert
            # nobody can act on.
            #
            # High interference is still worth surfacing BEFORE it bites, so
            # NOISE_ALERT keeps its own warn -- but it is a warn now, not an alert:
            # if clients are happy it is not an emergency. The number is always in the
            # table either way, so nothing is hidden, it just stops shouting.
            if judged is not None and judged < SAT_ALERT:
                state = "alert"
            elif judged is not None and judged < SAT_WARN:
                state = "warn"
            elif noise is not None and noise >= NOISE_ALERT:
                state = "warn"
            else:
                state = "ok"
            p.rows.append({
                "ap": d["name"], "band": r["band"], "channel": r["channel"],
                "util": r.get("util"), "interference": noise, "satisfaction": sat,
                # The figure the state was actually decided on, so the explanation and
                # the verdict can never disagree -- quoting the instantaneous 60% while
                # judging on a sustained 96% would be its own small lie.
                "judged": judged,
                "clients": r.get("clients"), "state": state,
            })

    busy = [r for r in p.rows if r["state"] != "ok"]
    if busy:
        def _why(r):
            j = r.get("judged")
            if j is not None and j < SAT_WARN:
                return f"{r['ap']} {r['band']} — clients at {j:.0f}% satisfaction (hourly)"
            return f"{r['ap']} {r['band']} — {r['interference']}% airtime from outside"
        p.readings.append(Reading(
            "Radio interference", f"{len(busy)} radio(s) degraded", as_of=now, group="wan",
            status=Status.WARN if all(r["state"] == "warn" for r in busy) else Status.ALERT,
            note="; ".join(_why(r) for r in busy[:3])))

    total = len(clients or [])
    nets = len({c["network"] for c in (clients or [])})
    if devices and [d for d in devices if d.get("state") != 1]:
        p.headline = "UniFi device offline"
    elif www and (www.get("status") != "ok"):
        p.headline = "Internet is down"
    elif busy:
        # It used to say "N APs healthy" while the chip beside it read ALERT.
        p.headline = (f"{len(busy)} of {len(p.rows)} radios degraded — "
                      f"{busy[0]['ap']} {busy[0]['band']}")
    else:
        p.headline = (f"{total} clients on {nets} networks · "
                      f"{len([d for d in (devices or []) if d.get('type') == 'uap'])} APs healthy")
    return p


# ---------------------------------------------------------------------------
# What wants attention
# ---------------------------------------------------------------------------
def attention_items(panels: list[Panel]) -> list[dict]:
    """Which cards want attention and why, for the banner at the top of the page.

    The banner said "Some things want attention" and then left you to scan nine cards
    to find out which. It knew perfectly well.

    The reason has to be built from the READINGS, not from the panel headline. A
    headline says what a thing is doing right now -- HVAC's is "Bedroom Minisplit
    cooling", which is true, current, and has nothing whatever to do with why that card
    is amber (a range hood filter at 7%). Only the readings know that.
    """
    order = {"alert": 0, "warn": 1, "unknown": 2}
    out = []
    for p in panels:
        rs = p.rollup()
        if rs is Status.OK:
            continue
        if p.error:
            why = p.error
        else:
            # informational readings are excluded from the roll-up, so they must not be
            # offered as the reason for it either.
            bad = [r for r in p.readings
                   if not r.informational and r.effective_status is not Status.OK]
            bad.sort(key=lambda r: -r.effective_status.rank)
            why = ", ".join(r.label for r in bad[:4]) or p.headline
        out.append({"key": p.key, "title": p.title, "status": rs.value, "why": why})
    out.sort(key=lambda d: order.get(d["status"], 9))
    return out


def trash_banner(today: dt.date | None = None) -> dict | None:
    """The bin reminder, as a banner line rather than a whole card.

    It was a full-width card spending most of its area on whitespace to carry one
    date that is only actionable for about twelve hours a week. So it now appears
    only when there is something to DO -- the day before pickup, and on pickup day
    itself -- and is absent the rest of the week.

    Normal collection is Wednesday, so in practice this shows up on a Tuesday. It is
    deliberately keyed to the pickup date rather than hardcoded to Tuesday, because
    holiday weeks slide collection to Thursday and a fixed Tuesday reminder would be
    wrong on exactly the weeks people forget.

    Returns None when there is nothing to say. The template renders nothing at all
    for None -- an empty banner is worse than no banner.
    """
    today = today or dt.date.today()
    payload = build_payload(today)
    pickup = dt.date.fromisoformat(payload["pickup_date"])
    days = (pickup - today).days
    if days > 1 or days < 0:
        return None

    note = payload.get("note") or ""
    # Same distinction the card drew: "schedule beyond <date> unverified" is a
    # statement about provenance, true every day once the anchor ages out, and is a
    # footnote rather than a reason to turn the banner amber. Anything else -- e.g.
    # "source calendar changed" -- is a real go-and-check.
    provenance = "unverified" in note
    return {
        "type": payload["type"],
        "day": payload["pickup_day"],
        "date": f"{pickup:%b %-d}",
        "today": days == 0,
        "status": "warn" if (note and not provenance) or payload["shifted"] else "info",
        "note": note,
        "shifted": payload["shifted"],
        "shift_reason": payload["shift_reason"] if payload["shifted"] else "",
    }


# ---------------------------------------------------------------------------
# Gaming progress
# ---------------------------------------------------------------------------
def gaming_panel(feed: tuple[float, dict] | None) -> Panel:
    """RetroAchievements + console progress, from GameLog's own hourly export.

    GameLog already writes this for Homepage, so Sagamore reads that file rather than
    querying RA/Xbox/PSN itself: no second set of credentials, no per-refresh upstream
    call, and one place to fix when a source changes shape.
    """
    p = Panel(key="gaming", title="Gaming Progress")
    if not feed:
        p.error = "GameLog feed unreachable"
        return p
    fetched_at, d = feed
    if not d:
        p.error = "GameLog returned nothing"
        return p

    # The export carries its own generation time. Trusting our fetch time instead would
    # hide a GameLog sync that died days ago while we kept re-reading the same file.
    gen = d.get("generated_epoch") or fetched_at
    ra = d.get("ra") or {}
    tot = d.get("totals") or {}

    brands = d.get("by_brand") or {}
    xb = d.get("xbox") or {}
    psn = d.get("psn") or {}

    pts, rank = ra.get("points"), ra.get("rank")
    p.headline = (f"{pts:,} RA points" if pts else "RetroAchievements") + \
                 (f" · rank #{rank:,}" if rank else "")

    # Three scoreboards, one per ecosystem, given equal width. They are deliberately
    # NOT added together: a RetroPoint, a Gamerscore point and a trophy are different
    # currencies, and a combined figure would be a number with no meaning.
    #
    # GameLog publishes gamerscore 0 when the OpenXBL profile call comes back without a
    # settings block (it happens intermittently, and it does not raise, so nothing is
    # logged). Nobody has a gamerscore of exactly zero, so 0 here means "not seen this
    # run" -- rendering it as 0 would be inventing a number, which is the one thing this
    # dashboard exists not to do.
    gs = xb.get("gamerscore") or None
    # GameLog now reuses the last good Gamerscore when OpenXBL answers without one, and
    # stamps it with when it was actually read. Age it from that rather than from the
    # feed's generation time: a cached figure is honest, a cached figure presented as
    # freshly read is not.
    gs_as_of = xb.get("gamerscore_as_of") or gen
    for label, value, source, games, mark, note_when_blank in (
        ("RetroPoints", ra.get("retro_points") or None, "RetroAchievements",
         ra.get("games"), "ra", ""),
        ("Gamerscore", gs, "Xbox", xb.get("games"), "g",
         "GameLog has never successfully read a gamerscore"),
        ("Trophies", psn.get("achievements") or None, "PlayStation", psn.get("games"),
         "trophy", ""),
    ):
        p.readings.append(Reading(
            label, f"{value:,}" if value else None,
            as_of=(gs_as_of if label == "Gamerscore" else gen),
            # A gamerscore read two days ago is still broadly true; one we have not
            # managed to read for a week is not something to keep asserting.
            max_age=(max(GAMING_MAX_AGE, 48 * 3600) if label == "Gamerscore"
                     else GAMING_MAX_AGE),
            group="score", mark=mark,
            note=(f"{source} · {games:,} games" if value and games
                  else (f"{source} — {note_when_blank}" if note_when_blank else source)),
        ))

    # "Mastered" used to be the RetroAchievements figure alone while sitting on a card
    # headed "Gaming Progress" next to all-platform totals, so it read as a whole-library
    # number and was not one. by_brand is a complete partition of the library (its games
    # and achievements sum exactly to `totals`), so summing it gives the real answer.
    # Mastered and beaten are two different achievements, and one line reading
    # "Mastered 102 · 206 beaten" made the second look like a footnote on the first.
    if brands:
        mastered = sum(int((v or {}).get("mastered") or 0) for v in brands.values())
        beaten = sum(int((v or {}).get("beaten") or 0) for v in brands.values())
        note = "every platform, RetroAchievements included"
    else:
        mastered, beaten = ra.get("mastered"), ra.get("beaten")
        note = "RetroAchievements only"
    p.readings.append(Reading("Mastered", mastered, as_of=gen, max_age=GAMING_MAX_AGE,
                              group="progress", mark="crown", note=note))
    p.readings.append(Reading("Beaten", beaten, as_of=gen, max_age=GAMING_MAX_AGE,
                              group="progress", mark="check", note=note))

    earned, total = tot.get("achievements"), tot.get("achievements_total")
    if earned and total:
        p.readings.append(Reading("Achievements", f"{earned:,} of {total:,}", as_of=gen,
                                  max_age=GAMING_MAX_AGE,
                                  note=f"{100.0 * earned / total:.1f}% of everything tracked"))
    if tot.get("hours"):
        p.readings.append(Reading("Hours played", f"{tot['hours']:,}", as_of=gen,
                                  max_age=GAMING_MAX_AGE,
                                  note=f"{tot.get('games', 0):,} games across every platform"))

    # One box each. As a single joined string this was four people's two numbers run
    # together on one line, and nobody could find their own.
    for who, stats in (d.get("family") or {}).items():
        p.readings.append(Reading(
            who, int((stats or {}).get("mastered") or 0), as_of=gen,
            max_age=GAMING_MAX_AGE, group="family", mark="crown",
            note=f"{int((stats or {}).get('beaten') or 0)} beaten"))

    # Per-platform table.
    for key, label in (("nintendo_platform", "Nintendo"), ("playstation_platform", "PlayStation"),
                       ("xbox_platform", "Xbox"), ("steam", "Steam"), ("switch", "Switch")):
        blk = d.get(key) or {}
        if not blk.get("games"):
            continue
        p.rows.append({"name": label, "games": blk.get("games"), "hours": blk.get("hours"),
                       "beaten": blk.get("beaten"), "mastered": blk.get("mastered"),
                       "achievements": blk.get("achievements")})

    # A feed that has stopped updating must not keep reporting yesterday's totals as today's.
    sw = d.get("switch") or {}
    if sw.get("feed_stale"):
        p.readings.append(Reading("Switch feed", "stale", as_of=gen, status=Status.WARN,
                                  note=f"no activity in {sw.get('days_since_activity', '?')} days"))
    return p


# ---------------------------------------------------------------------------
# Storage / shares
# ---------------------------------------------------------------------------
STORAGE_MAX_AGE = 6 * 60 * 60

# ZFS pools that are local scratch space rather than shares. They are pools like any
# other to ZFS, but nobody stores anything in them on purpose, so they belong in the
# collapsed local-storage list with the boot disks rather than on the face of the card
# next to tank. Scratch is a 186 GB mirror of two Intel DC S3610 SSDs on pve.
STORAGE_LOCAL_POOLS = {n.strip() for n in os.getenv("STORAGE_LOCAL_POOLS", "Scratch").split(",")
                       if n.strip()}

# Dataset -> what it is FOR. Without this the card is a wall of paths; the question people
# actually ask is "how much is Plex using", not "how big is tank/media".
# What a path is FOR. Matched longest-prefix-first, so a component directory folds into
# its app rather than appearing as its own line. Keys match dataset names AND mount paths.
_PROJECT = [
    ("tank/media/movies", "Movies"),
    ("tank/media/tv", "TV"),
    ("tank/media", "Other media"),
    ("tank/immich", "Immich photos"),
    ("tank/paperless", "Paperless documents"),
    ("tank/downloads", "Downloads"),
    ("tank/plex", "Plex config"),
    ("tank/ha-backups", "Home Assistant backups"),
    ("tank/backups", "Backups"),
    ("tank/office", "Office"),
    ("tank/realestate", "RealEstate"),
    ("tank/prowlarr", "Prowlarr"),
    ("tank/authentik", "Authentik"),
    # roms/ is the SOURCE library RomFleet ingests from and bios/ are the BIOS images its
    # emulated systems need — both are RomFleet, not separate projects.
    ("bulk/romfleet", "RomFleet"),
    ("bulk/roms", "RomFleet"),
    ("bulk/bios", "RomFleet"),
    ("bulk/zim", "Offline wiki archive"),
    ("bulk/timemachine", "Time Machine"),
    ("bulk/gaming-pc-backup", "PC backup"),
    ("bulk/youtube", "YouTube downloads"),
    # CT125 serves "APOLLO Translate" (LibreTranslate) and this dataset is its model
    # store — the offline-translation half of the archive, in its own container
    # rather than inside CT119. Same project, so the same line.
    ("bulk/translate", "Offline wiki archive"),
]


# --- Game collection (GameVault, CT122) -------------------------------------------
#
# GameVault runs five jobs on their own timers, and each figure on this card comes
# from exactly one of them. Ageing every reading against its own producer is the
# whole point: the shelf scan runs Mondays, so a count read on Thursday is four days
# old and still perfectly true, while a *price* four days old is not. One blanket
# max_age would either cry wolf about the count or wave through a stale valuation.
#
# (kind, label, max_age) -- max_age is one missed run plus slack, from the unit files
# on CT122: ingest 04:30, valuation 05:00 and enrich 05:40 daily; physical Mondays
# 06:30, with reconcile chained onto the end of it.
GV_JOBS = (
    ("ingest", "Catalogue sync", 36 * 3600),
    ("valuation", "Pricing run", 36 * 3600),
    ("enrich", "Artwork & metadata", 36 * 3600),
    ("physical", "Shelf scan", 9 * 86400),
    ("reconcile", "Reconcile", 9 * 86400),
)


def _iso_epoch(s: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def _usd(cents: object) -> str | None:
    """Whole dollars. Cents on a five-figure portfolio are noise, and the underlying
    figure is a scraped market estimate — rendering it to the penny would imply a
    precision the source does not have.

    Zero is returned as None, not "$0". Five of the holdings have no price yet, and a
    game GameVault has not managed to value is an unknown, not a worthless one.
    """
    try:
        v = float(cents)
    except (TypeError, ValueError):
        return None
    return f"${v / 100:,.0f}" if v else None


def _clip(text: str, limit: int = 74) -> str:
    """The shelf scan reports catalogue coverage as nine colon-separated pairs, which
    is a log line rather than a note. Everything else GameVault writes is a readable
    sentence and fits untouched."""
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip(" ;,") + "…"


def collection_panel(feed: tuple[float, dict, dict] | None) -> Panel:
    """The physical game collection — GameVault is the book of truth for it.

    This card asserts nothing of its own. Every number is one GameVault published,
    aged against the job that produced it, so a pricing run that quietly stopped
    three weeks ago shows as UNKNOWN rather than as a confident dollar figure. A
    stale number about money is the exact failure this dashboard exists to refuse.
    """
    p = Panel(key="collection", title="Game Collection")
    if not feed:
        p.error = "GameVault unreachable"
        return p
    _fetched_at, d, coll = feed
    if not d:
        p.error = "GameVault returned nothing"
        return p

    runs = d.get("runs") or {}
    latest = d.get("latest") or {}
    games = [g for g in (coll.get("games") or []) if isinstance(g, dict)]

    # Age from the run that produced the figure, never from our own fetch: re-reading
    # a number GameVault last recomputed on Monday does not make it Thursday's number.
    valued_at = _iso_epoch((runs.get("valuation") or {}).get("finished_at"))
    counted_at = _iso_epoch((runs.get("reconcile") or {}).get("finished_at")) or valued_at

    owned = d.get("num_holdings") or latest.get("num_owned")
    wanted = d.get("num_wanted")
    actual = latest.get("total_actual_c")

    # The dashboard endpoint has a by_system block, but every entry comes back with
    # n=None, and its platform counts are catalogue-wide in any case (7,709 Switch
    # games exist; eleven of them are his). Tally the holdings themselves instead.
    plats: dict[str, dict] = {}
    for g in games:
        row = plats.setdefault(g.get("platform_name") or "Unknown",
                               {"name": g.get("platform_name") or "Unknown",
                                "games": 0, "value_c": 0})
        row["games"] += int(g.get("quantity") or 1)
        row["value_c"] += int(g.get("value_c") or 0)
    cib = sum(1 for g in games if (g.get("condition") or "").lower() == "cib")

    p.headline = f"{owned:,} games on the shelf" if owned else "Game collection"
    if actual:
        p.sub = f"{_usd(actual)} at current prices" + (
            f" · {len(plats)} platforms" if plats else "")

    # Three figures, three currencies — a count, a dollar value and a wishlist — laid
    # out like the Gaming card's scoreboards rather than stacked as prose.
    # `status` carries the case staleness cannot: a run with no timestamp at all. as_of
    # None means `stale` is False, so without this an undateable figure would render as
    # confidently current -- the one thing this dashboard must never do.
    p.readings.append(Reading(
        "Owned", f"{owned:,}" if owned else None, as_of=counted_at,
        max_age=GV_JOBS[4][2], group="score",
        status=Status.OK if counted_at else Status.UNKNOWN,
        note=(f"{len(plats)} platforms · {cib} complete in box" if games
              else "held in GameVault")))
    p.readings.append(Reading(
        "Value", _usd(actual), as_of=valued_at, max_age=GV_JOBS[1][2], group="score",
        status=Status.OK if valued_at else Status.UNKNOWN,
        # Loose and sealed are the same shelf priced two other ways, so they belong
        # beside the headline figure rather than as rows of their own.
        note=" · ".join(x for x in (
            (f"loose {_usd(latest.get('total_loose_c'))}"
             if latest.get("total_loose_c") else ""),
            (f"sealed {_usd(latest.get('total_new_c'))}"
             if latest.get("total_new_c") else "")) if x) or "market estimate"))
    p.readings.append(Reading(
        "Wanted", f"{wanted:,}" if wanted else None, as_of=counted_at,
        max_age=GV_JOBS[4][2], group="score",
        status=Status.OK if counted_at else Status.UNKNOWN, note="on the wishlist"))

    # The pipeline behind all of the above. Folded away while it is healthy, because
    # nobody opens this card to read a job log -- but every one of these still scores
    # into the card's colour, so a dead timer surfaces without being hunted for.
    for kind, label, max_age in GV_JOBS:
        r = runs.get(kind)
        if not r:
            continue
        fin = _iso_epoch(r.get("finished_at"))
        ok = bool(r.get("ok"))
        detail = str(r.get("detail") or "").strip()
        p.readings.append(Reading(
            label,
            humanise_age(max(0.0, time.time() - fin)) if fin else None,
            as_of=fin, max_age=max_age, group="jobs",
            # A run that finished and reported failure is a different thing from one
            # that never ran: the first is a WARN we can describe, the second falls
            # through to UNKNOWN on age.
            status=Status.OK if ok else Status.WARN,
            note=(_clip(detail) if ok else (f"last run failed — {_clip(detail)}"
                                            if detail else "last run failed"))))

    for row in sorted(plats.values(), key=lambda r: -r["games"]):
        p.rows.append({"name": row["name"], "games": row["games"],
                       "value": _usd(row["value_c"])})

    # Most recent acquisitions. GameVault's own /api/dashboard `recent` block returns
    # system=None for every entry, so build it from the items, which carry the real
    # platform name.
    for g in sorted((g for g in games if g.get("acquired_at")),
                    key=lambda g: str(g.get("acquired_at")), reverse=True)[:6]:
        when = _iso_epoch(g.get("acquired_at"))
        p.app_rows.append({
            "title": g.get("title") or "—",
            "platform": g.get("platform_name") or "—",
            "value": _usd(g.get("value_c")),
            "when": (dt.datetime.fromtimestamp(when).strftime("%-d %b") if when else "—"),
        })

    return p


def _project_for(path: str) -> str:
    """Map a dataset name or a mount path to a project label."""
    key = path.lstrip("/")
    for prefix, label in _PROJECT:
        if key == prefix or key.startswith(prefix + "/"):
            return label
    return key



def _gb(n: float) -> str:
    if n >= 2 ** 40:
        return f"{n / 2 ** 40:.2f} TB"
    if n >= 2 ** 30:
        return f"{n / 2 ** 30:.0f} GB"
    return f"{n / 2 ** 20:.0f} MB"


def storage_panel(snap: tuple[float, dict] | None,
                  pve_storage: list | None = None) -> Panel:
    """Pool headroom, then what is actually consuming it — by project, not by dataset.

    Three things made the first version wrong, all worth stating:

    1. It reported each dataset's `used`, which INCLUDES its children — so tank/immich
       and tank/immich/data both read 77 GB for the same 77 GB. Now uses `self`
       (usedbydataset), so every byte is counted exactly once.
    2. It listed datasets, and several are internal components of one app — paperless has
       consume/data/media/postgres_data. Those roll up into "Paperless".
    3. The largest thing on the estate was invisible. /bulk/romfleet is an
       ordinary DIRECTORY, not a dataset, so 2.6 TB of ROM library appeared nowhere and the
       card reported RomFleet as 6 GB — the small `roms` dataset sitting next to it. The
       pusher now also walks pool roots one level deep.
    """
    p = Panel(key="storage", title="Storage & Shares")
    if not snap:
        p.readings.append(Reading("Datasets", None, as_of=None, status=Status.UNKNOWN,
                                  note="no storage report pushed yet — see "
                                       "sagamore-storage-push.timer"))
        p.headline = "Cannot assess — no report"
        return p
    pushed_at, rep = snap
    rows = rep.get("datasets") or []
    if (time.time() - pushed_at) >= STORAGE_MAX_AGE or not rows:
        p.readings.append(Reading("Datasets", None, as_of=pushed_at or None,
                                  status=Status.UNKNOWN,
                                  note=f"report is {(time.time() - pushed_at) / 3600:.0f}h old"))
        p.headline = "Cannot assess — report is stale"
        return p

    source = rep.get("source") or "pve"
    all_pools = [r for r in rows if "/" not in r["name"]]
    # Shares stay on the face of the card; local scratch pools drop into the collapsed
    # list and out of the headline, which is about where the estate's bulk data lives.
    pools = [r for r in all_pools if r["name"] not in STORAGE_LOCAL_POOLS]
    tight = []
    fullest = None
    for pool in sorted(all_pools, key=lambda r: r["name"]):
        local = pool["name"] in STORAGE_LOCAL_POOLS
        used, avail = pool.get("used", 0), pool.get("avail", 0)
        total = used + avail
        pct = (100.0 * used / total) if total else 0.0
        # ZFS slows down as a pool fills; 80% is the usual line to start caring.
        st = Status.OK if pct < 80 else (Status.WARN if pct < 90 else Status.ALERT)
        if pct >= 80:
            tight.append(pool["name"])
        if not local and (fullest is None or pct > fullest[1]):
            fullest = (pool["name"], pct)
        # The percentage is the value, because it is the number that carries the
        # judgement -- and it is the one the colour applies to. Free space is the
        # supporting detail, not the headline. Previously the card said the same
        # thing three times: free space as the value, used-of-total-and-percent as
        # the note, and free space again in the panel title.
        p.readings.append(
            Reading(f"{source} · {pool['name']}" if local else pool["name"],
                    f"{pct:.0f}%", as_of=pushed_at,
                    group="host" if local else "pool",
                    max_age=STORAGE_MAX_AGE, status=st, bar=pct,
                    note=f"{_gb(avail)} free of {_gb(total)}")
        )

    # ---- the hosts themselves ------------------------------------------------
    # Pools are the estate's bulk storage; these are the boot disks and VM stores the
    # nodes actually run from, and a node whose root fills up stops being a node. They
    # come from the Proxmox cluster resource list already polled for the homelab card,
    # so this costs no extra call. Shared entries (the PBS datastores) are skipped --
    # they are the same bytes seen from three nodes and would triple-count.
    LOCAL = {"local": "root", "local-lvm": "VM disks"}
    for r in sorted(pve_storage or [], key=lambda r: (r.get("node") or "", r.get("storage") or "")):
        if r.get("shared") or r.get("storage") not in LOCAL:
            continue
        used, total = r.get("disk") or 0, r.get("maxdisk") or 0
        if not total:
            continue
        pct = 100.0 * used / total
        st = Status.OK if pct < 80 else (Status.WARN if pct < 90 else Status.ALERT)
        p.readings.append(
            Reading(f"{r.get('node')} · {LOCAL[r['storage']]}", f"{pct:.0f}%",
                    as_of=pushed_at, group="host", status=st, bar=pct,
                    note=f"{_gb(total - used)} free of {_gb(total)}")
        )

    # Hosts that report themselves rather than being visible to Proxmox -- gaming-pc is a
    # desktop, not a cluster member. Absent until something pushes; a desktop that is
    # asleep simply has no line rather than a stale one.
    for h in (rep.get("hosts") or []):
        used, total = int(h.get("used") or 0), int(h.get("total") or 0)
        if not total:
            continue
        pct = 100.0 * used / total
        st = Status.OK if pct < 80 else (Status.WARN if pct < 90 else Status.ALERT)
        p.readings.append(
            Reading(f"{h.get('host')} · {h.get('label') or 'disk'}", f"{pct:.0f}%",
                    as_of=h.get("as_of") or pushed_at, group="host", bar=pct,
                    max_age=STORAGE_MAX_AGE, status=st,
                    note=f"{_gb(total - used)} free of {_gb(total)}")
        )

    # ---- roll everything up to a project -------------------------------------
    totals: dict[str, int] = {}

    def add(label: str, size: int) -> None:
        if size > 0:
            totals[label] = totals.get(label, 0) + size

    for r in rows:
        name = r["name"]
        if "/" not in name:
            continue                       # pool roots are handled by the dirs pass below
        add(_project_for(name), int(r.get("self") or 0))

    # A directory that IS a dataset mountpoint is already counted above. `du -x` still
    # stats it, so /tank/paperless arrived from both passes and would be added twice.
    mounts = {r.get("mount") for r in rows if r.get("mount")}
    for d in (rep.get("dirs") or []):
        path = d.get("path", "")
        if path in mounts:
            continue
        add(_project_for(path), int(d.get("used") or 0))

    p.rows = [{"project": k, "used": v, "human": _gb(v)}
              for k, v in sorted(totals.items(), key=lambda kv: -kv[1]) if v >= 2 ** 30]

    if tight:
        p.headline = f"{', '.join(tight)} over 80% full"
    elif fullest:
        p.headline = (f"{len(pools)} pool{'s' if len(pools) != 1 else ''} healthy — "
                      f"most used is {fullest[0]} at {fullest[1]:.0f}%")
    else:
        p.headline = "No pools reported"
    return p


# ---------------------------------------------------------------------------
# Homelab exposure
# ---------------------------------------------------------------------------
def homelab_panel(pve: dict | None, states: dict, cert_days: int | None,
                  patch: tuple[int, dict] | None = None,
                  apps: tuple[int, dict] | None = None,
                  playstations: list | None = None,
                  dead_devices: dict | None = None,
                  health: tuple[int, dict] | None = None) -> Panel:
    p = Panel(key="homelab", title="Homelab Exposure")
    if pve is None:
        p.error = "Proxmox API unreachable"
        return p

    # Console firmware. This is all that survived the Consoles card, because it was the
    # only thing on it that any console actually reports: the PS5 answers DDP with a
    # system-version, and nothing else does. Xbox exposes no version through any API
    # (verified 2026-08-21: no ICMP, no listening TCP port, no SSDP, no UDP 5050 — and
    # the Xbox Live cloud API, the most privileged access there is, carries power state
    # and storage but no build). The Switch is closed by design. Storage was on the card
    # too and reported for nothing: the PS5 exposes no capacity field, and the Xbox
    # sensors that did went away with its remote features.
    for ps in (playstations or []):
        ver = ps.get("version")
        if not ver:
            continue
        p.readings.append(
            Reading(f"{ps.get('host_type', 'PlayStation')} firmware", ver, as_of=time.time(),
                    informational=True,
                    note=f"{ps.get('name') or 'console'} — reported over DDP; no console "
                         f"publishes an update feed to compare it against")
        )

    nodes = pve.get("nodes") or []
    online = [n for n in nodes if n.get("status") == "online"]
    p.readings.append(
        Reading("Nodes online", f"{len(online)}/{len(nodes)}", as_of=time.time(),
                status=Status.OK if len(online) == len(nodes) else Status.ALERT)
    )

    # Templates are never "running" and would otherwise count as permanently down.
    guests = [g for g in (pve.get("guests") or []) if not g.get("template")]
    running = [g for g in guests if g.get("status") == "running"]
    stopped = [g for g in guests if g.get("status") != "running"]
    # Intent, not count. A guest stopped with onboot=1 was meant to come up and did
    # not — a crash or a failed start, and worth a human. One with onboot=0 was stood
    # down deliberately (a decommission, a parked service) and must not nag forever;
    # the old rule ("more than one stopped is a WARN") could not tell them apart, so
    # retiring a service left the panel permanently unhappy.
    # ⚠️ onboot missing entirely means the lookup FAILED, which is treated as
    # unexpected on purpose: unknown intent must not be silently excused.
    unexpected = [g for g in stopped if g.get("onboot") != 0]
    parked = [g for g in stopped if g.get("onboot") == 0]

    def _names(gs: list) -> str:
        return ", ".join(f"{g['vmid']} {g.get('name', '')}".strip() for g in gs)

    bits = []
    if unexpected:
        bits.append("down unexpectedly: " + _names(unexpected))
    if parked:
        bits.append(f"{len(parked)} stood down on purpose ({_names(parked)})")
    p.readings.append(
        Reading("Guests running",
                f"{len(running)}/{len(guests)}"
                + (f" · {len(parked)} stood down" if parked else ""),
                as_of=time.time(),
                status=Status.WARN if unexpected else Status.OK,
                note="; ".join(bits) or "all up")
    )

    # Patch debt. Preferred source is the pushed report from pve, which covers
    # hosts AND every container; the Proxmox API can only do hosts, and only
    # with Sys.Modify (a write permission this token deliberately lacks).
    # sagamore-patch-push.timer on pve runs every 3 hours (OnCalendar=*-*-*00/3:20:00).
    # This was 26h with a comment about "a daily push" -- left over from when it WAS daily,
    # and never tightened. The effect: a snapshot could be eight pushes out of date and still
    # render as a confident number. On 2026-09-03 the card showed 16 pending for three hours
    # after the updates had actually been applied, with nothing to say the figure was old.
    # 7h = two missed pushes plus slack, so a real outage still surfaces as UNKNOWN.
    PATCH_MAX_AGE = 7 * 3600
    if patch and (time.time() - patch[0]) < PATCH_MAX_AGE:
        pushed_at, report = patch
        targets = report.get("targets") or []

        # 🚨 HOSTS AND GUESTS ARE NOT THE SAME NUMBER. A guest is cleared by the 05:00
        # routine run, so anything above zero there means the automation is broken and is
        # worth an alarm. A host is Tier H -- NOAPPLY_HOST, deliberately never auto-applied
        # -- and can only be cleared by a planned rolling reboot, so a count there is
        # scheduled work, not a fault.
        #
        # Summing them warned every single morning about three hypervisors that nothing
        # was allowed to touch, which is how a card stops being read: the day guests DO
        # start piling up, that warning would have looked identical to a week of noise.
        # Host items are still raised -- by the Sunday digest, which reports Tier H and is
        # read deliberately rather than glanced at from bed. Nothing is lost by moving the
        # nag to the cadence that matches the work.
        #
        # `kind` is sent by sagamore-patch-push.sh since 2026-09-10; the name fallback is
        # for a payload pushed by an older copy of that script, not a guess we rely on.
        def _is_host(t):
            k = t.get("kind")
            if k:
                return k == "host"
            return not str(t.get("name", "")).startswith(("ct", "vm"))

        hosts = [t for t in targets if _is_host(t)]
        guests = [t for t in targets if not _is_host(t)]
        # 🚨 A PENDING GUEST UPDATE IS NOT NEWS UNTIL IT NEEDS SOMEONE (2026-09-11). This
        # card is fed every 3 hours and the 05:00 routine run applies what it can, so every
        # package that landed during the day used to turn it yellow until morning. Alex:
        # "notice of updates throughout the day, then only updating at night creates
        # alerts that can't be acted upon." A guest now warns only when its updates need
        # a human (gated -- update-manager's own rule for queueing them for approval) or
        # when a 05:00 run came and went without applying them. See app/updates.py.
        from .updates import missed_run
        now = time.time()
        routine = report.get("routine")

        def _gated(t):
            # Sent by sagamore-patch-push.sh since 2026-09-11. An older payload carries no
            # verdict; a pending reboot was gated then too, so that much can be inferred.
            g = t.get("gated")
            return bool(t.get("reboot")) if g is None else bool(g)

        def _installable(t):
            # Sent by sagamore-patch-push.sh since 2026-09-17: what apt would install RIGHT
            # NOW, from a simulated upgrade's `Inst` lines. Ubuntu's PHASED updates (released
            # to a percentage of machines at a time) and apt's KEPT-BACK ones (needing new
            # dependencies) sit in `packages` but never here. Counting those as overdue meant
            # saying "didn't apply at 05:00" about a run that had done everything it could —
            # CT108's nvidia set and CT114's netplan set, every morning. No key at all means
            # an older pusher: fall back to judging every package, as before.
            inst = t.get("installable")
            return set(inst) if isinstance(inst, list) else None

        def _overdue(t):
            inst = _installable(t)
            return any(missed_run(ts, pushed_at, routine, now)
                       for pkg, ts in (t.get("first_seen") or {}).items()
                       if inst is None or pkg in inst)

        behind = [t for t in guests if t.get("count")]
        needs_you = [t for t in behind if _gated(t)]
        missed = [t for t in behind if not _gated(t) and _overdue(t)]
        tonight = [t for t in behind if t not in needs_you and t not in missed]
        total_pending = sum(t.get("count", 0) for t in guests)
        actionable = sum(t.get("count", 0) for t in needs_you + missed)
        sec_pending = sum(t.get("security", 0) for t in guests)
        # A gated guest already says why; this is for a reboot with nothing else pending.
        reboots = [t["name"] for t in guests if t.get("reboot") and t not in needs_you]

        host_pending = sum(t.get("count", 0) for t in hosts)
        if host_pending:
            host_sec = sum(t.get("security", 0) for t in hosts)
            kernel = any("kernel" in pkg for t in hosts for pkg in (t.get("packages") or []))
            p.readings.append(Reading(
                "Host updates", host_pending, as_of=pushed_at, max_age=PATCH_MAX_AGE,
                status=Status.OK, informational=True,
                note=(f"{len([t for t in hosts if t.get('count')])} of {len(hosts)} nodes"
                      + (f", {host_sec} security" if host_sec else "")
                      + (" · includes a KERNEL, so a reboot each" if kernel else "")
                      + " · Tier H: manual rolling reboot, never automatic — raised in the "
                        "Sunday digest")))

        def _few(ts):
            names = [t["name"] for t in ts]
            return ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")

        def _count(t):
            """What the 05:00 run can actually install here, not the raw pending figure."""
            inst = _installable(t)
            return len(inst) if inst is not None else t.get("count", 0)

        # Everything pending that apt will NOT install right now: Ubuntu phasing it, or apt
        # keeping it back for new dependencies. Named on the card so "3 apply at 05:00" next
        # to a pending count of 13 is self-explaining rather than looking like a miscount.
        deferred = sum(max(0, t.get("count", 0) - _count(t)) for t in behind)
        bits = []
        if missed:
            bits.append(f"didn't apply at 05:00: {_few(missed)}")
        if needs_you:
            bits.append("needs you: " + "; ".join(
                f"{t['name']} ({t.get('gated_why') or 'reboot needed'})" for t in needs_you[:3]))
        if tonight:
            n = sum(_count(t) for t in tonight)
            if n:
                bits.append(f"{n} appl{'ies' if n == 1 else 'y'} at 05:00")
        if deferred:
            bits.append(f"{deferred} deferred by Ubuntu (phased or kept back)")
        p.readings.append(
            Reading("Pending updates", total_pending, as_of=pushed_at,
                    max_age=PATCH_MAX_AGE,
                    # Graded on what needs someone, not on the raw count: 12 packages that
                    # the 05:00 run will apply are fine; one it has already failed to is not.
                    status=(Status.OK if actionable == 0 else
                            Status.WARN if actionable < 40 else Status.ALERT),
                    # The scan time is stated outright rather than left implied. This number
                    # is a snapshot taken elsewhere on a 3-hourly timer, not something read
                    # live at page load, and "16" three hours after they were applied reads
                    # exactly like "16 right now" unless the card says when it was counted.
                    note=(("; ".join(bits) + " · ") if bits else "")
                         + f"{len(guests)} containers"
                         + (f", {sec_pending} security" if sec_pending else "")
                         + (f", reboot needed: {', '.join(reboots[:3])}" if reboots else "")
                         + f" · counted {dt.datetime.fromtimestamp(pushed_at):%H:%M}")
        )
        tag = {id(t): "needs you" for t in needs_you}
        tag.update({id(t): "missed 05:00" for t in missed})
        tag.update({id(t): "tonight" for t in tonight})
        p.rows = [
            {"node": t["name"], "count": t.get("count"),
             "packages": (t.get("packages") or [])[:12],
             "tag": "Tier H" if _is_host(t) else tag.get(id(t), "")}
            for t in sorted(targets, key=lambda t: -t.get("count", 0))
            if t.get("count")
        ] or [{"node": "all targets", "count": 0, "packages": []}]
    else:
        stale_note = ("no patch report has been pushed yet"
                      if not patch else
                      f"last patch report is {(time.time() - patch[0]) / 3600:.0f}h old")
        p.readings.append(
            Reading("Pending updates", None, as_of=None,
                    status=Status.UNKNOWN,
                    note=f"{stale_note} — see sagamore-patch-push.timer on pve")
        )
        p.rows = []

    # Self-hosted app versions. The apt scan cannot see these at all: they are
    # git checkouts, release tarballs, Docker tags and native servarr binaries.
    # pve already computes exactly this for the 08:00 Discord digest
    # (app-update.sh --dry-run), so it reports in rather than us re-deriving it.
    APPS_MAX_AGE = 26 * 3600
    if apps is not None:
        pushed_at, report = apps
        entries = [a for a in (report.get("apps") or [])
                   if str(a.get("name", "")).lower() not in APPS_IGNORE]
        if (time.time() - pushed_at) >= APPS_MAX_AGE or not entries:
            p.readings.append(
                Reading("Self-hosted apps", None, as_of=pushed_at or None,
                        status=Status.UNKNOWN,
                        note=(f"last app report is {(time.time() - pushed_at) / 3600:.0f}h old"
                              " — see sagamore-app-push.timer on pve"))
            )
        else:
            # Same rule as the guest packages above (2026-09-11): an app that will update
            # itself is not news until it plainly has not. `applies` comes from
            # sagamore-app-push.sh, read off app-update.sh's own wording: "tonight" (the
            # 05:00 run applies it), "self" (the app's built-in updater, on its own clock)
            # or "you". A payload without it counts as "you" -- an update we cannot
            # classify is never hidden.
            from .updates import missed_run
            now = time.time()
            routine = report.get("routine")
            pending = [a for a in entries if a.get("state") == "update"]
            failed = [a for a in entries if a.get("state") == "failed"]
            blind = [a for a in entries if a.get("state") == "unknown"]
            held = [a for a in pending if a.get("held")]

            def _auto(a):
                return a.get("applies") in ("tonight", "self") and not a.get("held")

            missed = [a for a in pending if _auto(a) and missed_run(
                a.get("first_seen"), pushed_at, routine, now,
                runs_nightly=a.get("applies") == "tonight")]
            later = [a for a in pending if _auto(a) and a not in missed]
            needs_you = [a for a in pending if not _auto(a)]
            if failed:
                st = Status.ALERT
            elif needs_you or missed:
                st = Status.WARN
            elif blind:
                st = Status.UNKNOWN
            else:
                st = Status.OK
            note_bits = []
            if failed:
                note_bits.append("failed: " + ", ".join(a["name"] for a in failed[:3]))
            if missed:
                note_bits.append("didn't apply at 05:00: "
                                 + ", ".join(a["name"] for a in missed[:4]))
            if needs_you:
                note_bits.append("needs you: " + ", ".join(a["name"] for a in needs_you[:4]))
            nightly = [a["name"] for a in later if a.get("applies") == "tonight"]
            selfup = [a["name"] for a in later if a.get("applies") == "self"]
            if nightly:
                note_bits.append(", ".join(nightly[:4])
                                 + (" applies" if len(nightly) == 1 else " apply") + " at 05:00")
            if selfup:
                note_bits.append(", ".join(selfup[:4])
                                 + (" updates itself" if len(selfup) == 1
                                    else " update themselves"))
            if held:
                note_bits.append(f"{len(held)} held for manual")
            if blind:
                note_bits.append(f"{len(blind)} unreadable")
            # ⚠️ "all 11 current" read as "the estate is current". It is not: the pusher
            # only knows the apps app-update.sh has handlers for, and the estate runs ~3x
            # that many guests. Naming the coverage stops a partial answer masquerading as
            # a complete one — the same reason an unobservable reading renders `unknown`.
            tracked = len(entries)
            guest_total = len([g for g in (pve.get("guests") or [])
                               if g.get("status") == "running" and not g.get("template")])
            # Only meaningful when there is genuinely a gap; "2 of 1 guests tracked" is
            # nonsense, and a suffix that appears unconditionally stops being read.
            gap = max(0, guest_total - tracked)
            value = (f"{len(pending)} of {tracked} have updates"
                     if pending else f"all {tracked} current")
            # Coverage belongs on the meta line, not appended to the value. These
            # readings sit in a ~15rem column; "1 of 17 have updates · 17 of 34 guests
            # tracked" was far too long for it, so the value took the whole row and
            # squeezed the label into a vertical stack of words. The meta line spans
            # the full width of the card and is the right home for a qualifier.
            if gap:
                note_bits.append(f"{tracked} of {guest_total} guests tracked; "
                                 f"{gap} have no app-level version tracking")
            p.readings.append(
                Reading("Self-hosted apps", value, as_of=pushed_at,
                        max_age=APPS_MAX_AGE, status=st,
                        note="; ".join(b for b in note_bits if b)
                             or "every tracked app on its latest release")
            )
            # Worst first, so the thing that needs a human is at the top. An update that
            # applies itself is `scheduled` -- listed, but below everything that is not fine.
            state_of = {id(a): "missed" for a in missed}
            state_of.update({id(a): "scheduled" for a in later})
            order = {"failed": 0, "missed": 1, "update": 2, "unknown": 3, "scheduled": 4, "ok": 5}
            rows = []
            for a in entries:
                st_ = state_of.get(id(a), a.get("state", "unknown"))
                rows.append({
                    "name": a.get("name", "?"), "state": st_,
                    "word": ("self-updating" if st_ == "scheduled"
                             and a.get("applies") == "self" else ""),
                    "current": a.get("current") or "", "latest": a.get("latest") or "",
                    "detail": a.get("detail") or "", "managed": a.get("managed") or ""})
            p.app_rows = sorted(rows, key=lambda r: (order.get(r["state"], 9), r["name"]))

    # Does the app actually WORK? (2026-09-12)
    #
    # A version number is not health. OnlyOffice document editing was broken from
    # 2026-08-30 to 2026-09-12 while this card happily reported Nextcloud current and its
    # container up: nothing here asked whether a document could be opened. pve now runs
    # each service's OWN diagnostics (`occ setupchecks`, `occ onlyoffice:documentserver
    # --check`, background-job age) and pushes the verdicts to /api/ingest/health.
    #
    # `fail` means broken and needs a human -- that warns. `warn` is the service's own
    # advice (a missing security header) and `info` is context (65 checks ran): both ride
    # along in the note so the card never goes yellow over housekeeping. Each service
    # carries its own timestamp, so a dead pusher degrades that service to unknown rather
    # than quietly reporting the last good answer for ever.
    HEALTH_MAX_AGE = 70 * 60          # two missed 15-minute pushes, plus slack
    if health is not None:
        _, hreport = health
        services = (hreport or {}).get("services") or {}
        fresh, stale = {}, []
        for name, s in services.items():
            if (time.time() - (s.get("as_of") or 0)) < HEALTH_MAX_AGE:
                fresh[name] = s
            else:
                stale.append(name)
        checks = [(n, c) for n, s in fresh.items() for c in (s.get("checks") or [])]
        newest = max((s.get("as_of") or 0) for s in fresh.values()) if fresh else None
        if not checks:
            p.readings.append(Reading(
                "Service checks", None, as_of=newest,
                status=Status.UNKNOWN,
                note=(f"health report stale for: {', '.join(sorted(stale))}" if stale
                      else "no health report pushed yet")
                     + " — see sagamore-health-push.timer on pve"))
        else:
            failing = [(n, c) for n, c in checks if c.get("state") == "fail"]
            advisory = [c for _, c in checks if c.get("state") == "warn"]
            bits = []
            if failing:
                bits.append("; ".join(f"{n} {c.get('label') or c.get('key')}: "
                                      f"{str(c.get('detail') or '')[:90]}"
                                      for n, c in failing[:3]))
            if stale:
                bits.append(f"stale: {', '.join(sorted(stale))}")
            if advisory:
                bits.append(f"{len(advisory)} advisory")
            p.readings.append(Reading(
                "Service checks",
                f"{len(failing)} failing" if failing else f"all {len(checks)} passing",
                as_of=newest, max_age=HEALTH_MAX_AGE,
                status=(Status.WARN if failing else
                        Status.UNKNOWN if stale else Status.OK),
                note="; ".join(bits)
                     or f"{', '.join(sorted(fresh))} answering every check"))

    # Devices that have stopped answering.
    #
    # This is all that survived the Home Assistant card. Its other half -- pending core
    # and add-on updates -- was already reported here as "HA / firmware updates", so the
    # card was showing the same fact twice. Dead devices were not duplicated anywhere,
    # and are the more important half: a device that quietly stops reporting is the
    # failure this whole project exists to catch.
    #
    # A device counts as not responding ONLY when EVERY one of its entities is
    # unavailable. Counting any device with *some* unavailable entities produced 19
    # entries that were almost all noise: UniFi Network (72 of 104), Basement Switch
    # (27 of 40) and the phone (20 of 30) are all demonstrably reachable, and their
    # unavailable entities are disabled, permission-gated, or belong to another platform
    # entirely (the phone's kiosk_* and sim_* sensors are Android-only, on an iPhone).
    dead, partly = {}, 0
    for name, counts in (dead_devices or {}).items():
        bad, total = counts if isinstance(counts, (list, tuple)) else (counts, counts)
        if _ignored_device(name):
            continue
        if total and bad >= total:
            dead[name] = bad
        elif bad:
            partly += 1
    if dead_devices is not None:
        worst_first = sorted(dead.items(), key=lambda kv: -kv[1])
        p.readings.append(Reading(
            "Devices not responding", len(dead), as_of=time.time(),
            status=Status.ALERT if dead else Status.OK,
            note=(", ".join(f"{k} ({n})" for k, n in worst_first[:6]) if dead
                  else (f"every device answering · {partly} have some idle entities, "
                        f"which is normal" if partly else "every device answering")),
        ))
        p.dead_rows = [{"device": k, "entities": n} for k, n in worst_first]

    # Backups: prove they ran, don't assume.
    worst_age, failed = None, []
    for node, b in (pve.get("backups") or {}).items():
        if not b or not b.get("starttime"):
            failed.append(f"{node}: no record")
            continue
        age_h = (time.time() - b["starttime"]) / 3600
        worst_age = age_h if worst_age is None else max(worst_age, age_h)
        if b.get("status") != "OK":
            failed.append(f"{node}: {b.get('status')}")
    p.readings.append(
        Reading("Last backup", f"{worst_age:.0f}h ago" if worst_age is not None else None,
                as_of=time.time(),
                status=(Status.ALERT if failed else
                        Status.OK if (worst_age or 0) < 30 else Status.WARN),
                note="; ".join(failed) if failed else "all nodes reported OK")
    )

    if cert_days is not None:
        p.readings.append(
            Reading("TLS certificate", f"{cert_days}d", as_of=time.time(),
                    status=Status.OK if cert_days > 21 else
                    (Status.WARN if cert_days > 7 else Status.ALERT),
                    note="until wildcard expiry")
        )

    # Home Assistant's own update entities cover the HAOS VM and UniFi firmware,
    # neither of which the apt scan can see. Count only the ones HA can INSTALL
    # (supported_features bit 1), the same filter HA's Settings > Updates page uses.
    # Integrations like Plex also expose a notify-only update entity for a server that
    # lives elsewhere; counting it here read as an HA update that HA's own UI did not
    # show (Plex 1.43.4, 2026-09-10). Plex is reported with the self-hosted apps.
    pending_ha = [e for e, s in (states or {}).items()
                  if e.startswith("update.") and s.get("state") == "on"
                  and int(s.get("attributes", {}).get("supported_features") or 0) & 1]
    if states:
        p.readings.append(
            Reading("HA / firmware updates", len(pending_ha), as_of=time.time(),
                    status=Status.OK if not pending_ha else Status.WARN,
                    note=", ".join(_friendly(e) for e in pending_ha[:4]) or "everything current")
        )

    alerts = [r for r in p.readings if r.effective_status is Status.ALERT]
    warns = [r for r in p.readings if r.effective_status is Status.WARN]
    blind = [r for r in p.readings if r.effective_status is Status.UNKNOWN]
    if alerts:
        p.headline = "; ".join(f"{r.label}: {r.display()}" for r in alerts)
    elif warns:
        p.headline = f"{len(warns)} item(s) want attention — {warns[0].label.lower()}"
    elif blind:
        # Never claim an all-clear over data we could not read. Saying
        # "no open exposure" while a check silently failed is precisely the
        # false confidence this whole project exists to eliminate.
        p.headline = f"Cannot fully assess — {blind[0].label.lower()} unreadable"
    else:
        p.headline = "No open exposure — patched, backed up, certificates valid"
    if blind:
        p.sub = "Blind spots: " + ", ".join(r.label for r in blind)
    return p


# ---------------------------------------------------------------------------
# Now Playing
# ---------------------------------------------------------------------------
# How long a `paused` device still counts as "in use". A speaker paused since
# yesterday's Home Assistant restart is idle in every sense that matters; only
# a recent pause is someone stepping away mid-track.
#
# This was two hours, which is far too generous in practice: a Sonos or an Apple TV
# paused mid-morning still claimed the card at lunchtime, and "Now Playing" is meant
# to answer what is happening NOW. Twenty minutes covers stepping away for a coffee
# without letting yesterday's paused track masquerade as activity.
# Override with SAGAMORE_PAUSE_GRACE_MIN (minutes) in /etc/sagamore/env.
PAUSE_GRACE = int(os.getenv("SAGAMORE_PAUSE_GRACE_MIN", "20")) * 60

# Base URL the BROWSER uses to fetch Home Assistant artwork. entity_picture is a
# relative path carrying its own signed token, so it needs no auth header -- but it
# does need a host the viewer can actually reach. HA_URL is the internal address and
# is useless to anyone off the LAN, so this is overridable: set HA_PUBLIC_URL to the
# externally reachable URL (verified 2026-08-30: the signed proxy path returns 200
# image/jpeg through it with no authentication).
# Players that are never "now playing" in any sense worth a dashboard row. A sleep-sound
# machine running white noise all night is a media_player by type and pure noise by intent;
# it would sit on the card every night saying nothing. Comma-separated entity ids.
# Brand marks for the device-type line. Keyed on the pretty name media_kinds
# produces, so anything unmapped (Cast, DLNA, a bare "Media") renders as text --
# a missing logo must never blank the line.
BRAND_LOGOS = {"Sonos": "sonos", "Apple TV": "apple",
               "PlayStation": "playstation", "Xbox": "xbox"}

# Which door-class contacts are actually doors, and what to call them.
#
# Home Assistant reports ten entities of device_class "door" for a house with four:
# the contacts were all installed from one device profile, so the dining room windows
# and the office window are all labelled "door" too. Nothing in the data distinguishes
# them -- it is knowledge about the building, which is what this environment is for.
# Anything of device_class "door" that is NOT listed here is treated as a window.
#   SECURITY_DOORS=binary_sensor.front_door_entry_door=Front Door (Foyer),...
SECURITY_DOORS: dict[str, str] = {}
for _e in os.getenv("SECURITY_DOORS", "").split(","):
    if "=" in _e:
        _k, _v = _e.split("=", 1)
        SECURITY_DOORS[_k.strip()] = _v.strip()

NOWPLAYING_EXCLUDE = {e.strip() for e in
                      os.getenv("NOWPLAYING_EXCLUDE", "").split(",") if e.strip()}

HA_PUBLIC = (os.getenv("HA_PUBLIC_URL")
             or os.getenv("HA_URL", "http://192.168.1.20:8123")).rstrip("/")

# Apps that pve reports but which no longer warrant a line on the homelab card. Empty
# by default: Homepage was the only entry, and it is now decommissioned rather than
# merely hidden, so pve does not report it at all. The mechanism stays for the next one.
APPS_IGNORE = {a.strip().lower() for a in os.getenv("APPS_IGNORE", "").split(",")
               if a.strip()}

# What a console actually IS and where it sits. Home Assistant cannot tell us: the
# Xbox integration's device is the *Xbox Network account* ("Yukon9Cornelius", model
# "Xbox Network"), not the box in the room -- an account can sign in on any console,
# so HA is right not to claim one. That makes "Xbox Series X, in the office" a fact
# about this house, which is what the environment is for.
#   NOWPLAYING_DEVICES=yukon9cornelius=Xbox Series X|Office
NOWPLAYING_DEVICES: dict[str, tuple[str, str]] = {}
for _e in os.getenv("NOWPLAYING_DEVICES", "").split(","):
    if "=" in _e:
        _k, _v = _e.split("=", 1)
        _model, _, _area = _v.partition("|")
        NOWPLAYING_DEVICES[_k.strip()] = (_model.strip(), _area.strip())


def _art(url: str | None) -> str | None:
    """Absolute artwork URL, or None. PSN hands back a full CDN URL already."""
    if not url:
        return None
    return url if url.startswith(("http://", "https://")) else f"{HA_PUBLIC}{url}"

ACTIVE_STATES = {"playing", "buffering"}


def _now_playing_text(attrs: dict) -> str:
    """One line describing what is on, whatever kind of media it is.

    ⚠️ Stream metadata arrives HTML-ENCODED and must be decoded here. HOT 96.9 sends
    `media_artist = 'Fat Joe f/ Ashanti &amp; Ja Rule'` and `media_title = "What&apos;s
    Luv"`, HA passes that through verbatim, and the template escapes it again — so the
    card read "Jay-Z &amp; Lil" on 2026-09-12. Decoded ONCE, at the point the line is
    built.
    🚫 Do NOT fix this in the template with `|safe`: this text is whatever a radio
    station chose to put in its metadata, and marking it safe would hand that straight
    into the page. Autoescaping stays on; the string it escapes is simply correct now.
    A title with a bare `&` ("feat. DEAN & dj friz") is unaffected — unescape leaves it
    alone.
    """
    def dec(v):
        return html.unescape(v) if isinstance(v, str) else v

    title = dec(attrs.get("media_title") or "")
    artist = dec(attrs.get("media_artist") or "")
    series = dec(attrs.get("media_series_title") or "")
    app = dec(attrs.get("app_name") or attrs.get("source") or "")
    if series and title:
        return f"{series} — {title}"
    if title and artist:
        return f"{title} — {artist}"
    return title or app or "playing"


def _np_title_key(t: str) -> str:
    """Loose title key for matching a console's reported title to GameLog.

    Deliberately NOT a copy of GameLog's C.norm: both sides of the comparison are
    put through THIS function, so the two normalisations never have to agree. A
    copy would drift the first time GameLog changed its own.
    """
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    t = t.lower().replace("&", " and ").replace("'", "")
    t = re.sub(r"\bthe\b", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def now_playing_panel(states: dict,
                      kinds: dict | None = None,
                      app_icons: dict | None = None,
                      areas: dict | None = None,
                      progress: dict | None = None,
                      esde: tuple[int, dict] | None = None,
                      pause_since: dict | None = None) -> Panel:
    """What is actually on, right now — idle devices are hidden entirely.

    The house has 21 media players. Listing them all just to say "off" 17 times
    buries the one that matters, so this shows only what is genuinely in use and
    says so plainly when nothing is.

    Sonos speakers playing together are collapsed into a single row via
    `group_members`; four speakers on one track is one thing happening, not four.

    Consoles come from Home Assistant like everything else. This used to take a
    second, local feed of DDP-probed PlayStations as well, which put the PS5 on the
    card twice whenever it was mid-game -- once from the integration and once from
    the probe. The probe is still collected, but only the homelab card uses it now,
    for firmware. One device, one source of truth about what it is doing.
    """
    p = Panel(key="nowplaying", title="Now Playing")
    kinds = kinds or {}
    app_icons = app_icons or {}
    areas = areas or {}
    # A console running a game is as much "now playing" as a TV running a film, and it was
    # the one thing this card could not tell you — the Consoles panel showed power state and
    # storage but never what was on.
    states = states or {}
    now = time.time()
    rows: list[dict] = []
    seen: set[str] = set()

    # ⚠️ Nothing playing and "we could not ask" produce the same empty list, and the
    # panel is now HIDDEN when idle (see build_panels). Without this an HA outage would
    # make the card silently vanish and read as a quiet house. No states at all means
    # the poll failed or has not run — say so instead of asserting silence.
    if not states:
        p.error = "Home Assistant unreachable — cannot tell what is playing"
        # ⚠️ Do NOT return here. ES-DE is pushed straight to Sagamore and does not go
        # through Home Assistant at all, so a game genuinely known to be running must
        # still show when HA is the thing that is down. The error stays, because the
        # media players really are unknown -- the card then says both true things at
        # once instead of hiding the one fact it still has. Every loop below is a
        # no-op on empty states, so falling through is safe.

    for eid, st in sorted(states.items()):
        if not eid.startswith("media_player.") or eid in seen:
            continue
        if eid in NOWPLAYING_EXCLUDE:
            continue
        state = st.get("state")
        attrs = st.get("attributes", {})
        if state not in ACTIVE_STATES and state != "paused":
            continue
        if state == "paused":
            # Only a *recent* pause counts as in use — dated by SAGAMORE's record of when
            # the pause started (main.py → carry_pause_since), not by HA's clock.
            # 🚫 HA CANNOT DATE A PAUSE. A restart or integration reload rewrites
            # `last_changed` AND `media_position_updated_at` for every entity: on
            # 2026-09-12 HA restarted at 14:49:53 and restamped all 2112 entities, so the
            # Office Apple TV — paused since 05:03 the previous morning — read as 15
            # minutes old and had held this card for a day and a half against a
            # 20-minute grace. HA's own timestamps are the fallback only until our first
            # poll has recorded the pause.
            ps = (pause_since or {}).get(eid)
            ts = ps.get("since") if isinstance(ps, dict) else None
            if ts is None:
                ts = parse_ha_time(attrs.get("media_position_updated_at")) or \
                     parse_ha_time(st.get("last_changed"))
            if ts is None or (now - ts) > PAUSE_GRACE:
                continue

        members = [m for m in (attrs.get("group_members") or []) if m in states]
        group = [m for m in members if m != eid]
        seen.update(members or [eid])

        where = _friendly(eid, states)
        if group:
            where += " +" + str(len(group))
        rows.append({
            "where": where,
            "also": [_friendly(m, states) for m in group],
            "what": _now_playing_text(attrs),
            "state": state,
            "kind": kinds.get(eid, "media"),
            "dev": kinds.get(eid, "Media"),
            "logo": BRAND_LOGOS.get(kinds.get(eid, "")),
            "loc": areas.get(eid, ""),
            # Line 3 is "where · what exactly". For a grouped speaker that is the other
            # rooms it is playing to; the count that used to ride on the device line as
            # "+3" said the same thing less precisely, and next to a brand mark it read
            # as part of the logo.
            "detail": ("with " + ", ".join(_friendly(m, states) for m in group)) if group else "",
            # No artwork is normal for a live channel on an Apple TV; the app icon
            # says which app is on without pretending to be art for the programme.
            "art": _art(attrs.get("entity_picture")) or app_icons.get(attrs.get("app_id")),
            # Carried only as far as the de-duplication pass below, then dropped.
            # ⚠️ NOT `num()` -- that takes a STATE DICT and reads ["state"] from it, so
            # handed a bare number it returns None and the de-duplication silently
            # never fires. These are plain attribute values.
            "_dur": _secs(attrs.get("media_duration")),
            "_pos": _secs(attrs.get("media_position")),
        })

    # ---- one stream, one row -------------------------------------------------
    # An Apple TV playing from Plex is reported TWICE, by two integrations:
    #
    #   media_player.living_room                        Apple TV integration
    #       "Living Room Apple TV" · app_name Plex · has an AREA
    #   media_player.plex_plex_for_apple_tv_apple_tv_4  Plex integration
    #       "Plex (Plex for Apple TV - Apple TV)" · no area, no device kind
    #
    # Same film, same screen, one thing happening. This is the same double-report
    # the PS5 had from DDP plus PSN, and it gets the same answer: one device, one
    # source of truth about what it is doing.
    #
    # ⚠️ JOIN ON THE STREAM, NOT THE TITLE. Plex appends the year, so the titles
    # genuinely differ -- "Minions & Monsters" vs "Minions & Monsters (2026)" --
    # and any name-matching heuristic would miss it. `media_duration` is identical
    # and `media_position` agrees to within a second, because both are reading the
    # same playback. Requiring BOTH keeps two different programmes that happen to
    # share a runtime from being collapsed into one.
    #
    # Keep whichever report knows WHERE it is playing: a room is the useful fact on
    # a house dashboard, and the Plex session entity has no area, which is why the
    # duplicate row rendered with the bare fallback device label "Media".
    def _stream_rank(r: dict) -> tuple:
        return (bool(r.get("loc")), r.get("dev") != "Media", bool(r.get("logo")))

    deduped: list[dict] = []
    for r in rows:
        twin = None
        for q in deduped:
            if (r["_dur"] and q["_dur"] == r["_dur"]
                    and r["_pos"] is not None and q["_pos"] is not None
                    and abs(r["_pos"] - q["_pos"]) <= STREAM_MATCH_SECONDS):
                twin = q
                break
        if twin is None:
            deduped.append(r)
        elif _stream_rank(r) > _stream_rank(twin):
            deduped[deduped.index(twin)] = r
    for r in deduped:
        r.pop("_dur", None)
        r.pop("_pos", None)
    rows = deduped

    # ---- consoles ----
    # PlayStation comes from DDP, not Home Assistant — included on the same terms
    # as everything else: only when it is actually PLAYING something.
    #
    # CORRECTION 2026-08-30: this PS5 does NOT report the running title over DDP.
    # Captured mid-game (a title actually running, console `200 Ok`), the whole
    # response is seven fields:
    #
    #     HTTP/1.1 200 Ok / host-id / host-type / host-name / host-request-port
    #     device-discovery-protocol-version / system-version
    #
    # No `running-app-name`, no `running-app-titleid`. The earlier note here claimed
    # a field-less 200 meant "sitting on the Home Screen"; that was inferred from the
    # PS4 behaviour and never tested with a game running. On PS5 firmware 13.60 the
    # title is simply never exposed, so `running_app` is always "".
    #
    # Consequence: the PS5 can only ever contribute "awake vs rest mode" here. A real
    # Now Playing title needs the PSN cloud (Home Assistant's `playstation_network`
    # integration), not DDP. The loop below is kept because it costs nothing and will
    # start working if a future firmware does expose the field.
    #
    # This used to append the PS5 TWICE: once unconditionally whenever it was
    # awake (with the misleading filler "on, no title reported") and then again
    # here when a title existed. The duplicate was invisible while the console sat
    # idle and would only have surfaced mid-game.
    # PlayStation is NOT read here. Home Assistant's playstation_network integration
    # already provides media_player.office_playstation_5_pro with media_title straight
    # from the PSN cloud, and it is picked up by the media loop above like any other
    # player. The DDP discovery that used to sit here asked the console itself, which
    # never reports a running title (firmware 13.60 returns seven fields, none of them
    # the game) -- and a second PSN source only re-created the double-append this panel
    # has been bitten by before. DDP is still discovered for the firmware version, which
    # Homelab Exposure reads and PSN does not carry.

    # Xbox, via Xbox Live presence rather than the console itself.
    #
    # media_player.office_xbox and sensor.office_xbox_*_space_internal_storage disappear
    # ENTIRELY when "Enable remote features" is turned off on the console, and they do not
    # come back even while a game is running — verified 2026-08-29 with a game in progress.
    # Xbox Live presence keeps reporting throughout, so it is the only source that survives
    # that setting, and it carries the title the local media_player never did.
    #
    # Keyed on the exact `_now_playing` suffix AND its matching `_in_game` flag, never a
    # scan for sensor.*xbox* — that scan is the bug in the note below, which matched the
    # storage sensor and cheerfully reported "802" as the game being played.
    for eid, st in sorted(states.items()):
        if not eid.startswith("sensor.") or not eid.endswith("_now_playing"):
            continue
        base = eid[len("sensor."):-len("_now_playing")]
        if (states.get(f"binary_sensor.{base}_in_game") or {}).get("state") != "on":
            continue
        title = str(st.get("state") or "").strip()
        # in_game can go true a beat before the title lands; "unknown" is not a game.
        if not title or title.lower() in ("unknown", "unavailable", "none"):
            continue
        img = (states.get(f"image.{base}_now_playing") or {}).get("attributes", {})
        model, area = NOWPLAYING_DEVICES.get(base, ("", ""))
        rows.append({"where": "Xbox", "also": [], "what": title,
                     "state": "playing", "kind": "game",
                     "dev": "Xbox",
                     # The registry area wins if someone ever sets one; the configured
                     # area is the fallback, because today there is nothing to read.
                     "loc": areas.get(eid) or area,
                     "detail": model,
                     "logo": BRAND_LOGOS["Xbox"],
                     # the box art is on the sibling image entity, not the sensor
                     "art": _art(img.get("entity_picture"))})

    # NB: no console-sensor scan here. An earlier version walked sensor.*xbox* for a
    # title and matched sensor.office_xbox_total_space_internal_storage, so the card
    # cheerfully reported "802" as the game being played. The Xbox block above is
    # deliberately anchored to an exact suffix pair for that reason.
    # ---- ES-DE (retro gaming on AbrahamLincoln) -----------------------------
    # PUSHED, not polled, and for the same reason pve pushes its patch report: the
    # only thing that knows a libretro core just started is ES-DE itself. It fires a
    # `game-start` custom event script, which POSTs here; `game-end` and `quit` clear it.
    #
    # ⚠️ STALENESS IS THE WHOLE RISK. A clean exit clears the snapshot, but a crash, a
    # power cut or a pulled plug never sends `game-end` -- and a card that cheerfully
    # says "playing Sonic" three days later is worse than saying nothing. So the row is
    # bounded by ESDE_MAX_AGE regardless of what the last push claimed, exactly like a
    # stale sensor reading elsewhere on this dashboard.
    if esde:
        ts, e = esde
        if (e or {}).get("state") == "playing" and (time.time() - ts) <= ESDE_MAX_AGE:
            game = str(e.get("game") or "").strip()
            if game:
                rows.append({
                    "where": "ES-DE", "also": [], "what": game,
                    "state": "playing", "kind": "game", "dev": "ES-DE",
                    "logo": BRAND_LOGOS.get("ES-DE"),
                    "loc": str(e.get("system_full") or e.get("system") or ""),
                    "detail": "",
                    # Served by Sagamore rather than inlined: box art runs to a few
                    # hundred KB and this page re-renders on every poll. `v` is the
                    # push timestamp, so the browser re-fetches on a new game and
                    # caches within one.
                    "art": f"/api/esde/art?v={ts}" if e.get("art") else None,
                })

    p.rows = rows
    for r in rows:
        p.readings.append(
            Reading(r["where"], r["what"], as_of=now,
                    note=(("also on " + ", ".join(r["also"])) if r["also"] else
                          ("paused" if r["state"] == "paused" else r["kind"])))
        )

    # Trophies / achievements on the console rows, from GameLog's export. One pass
    # over the finished rows rather than at each append, so PlayStation and Xbox are
    # treated identically however their titles arrived.
    # ⚠️ A title we cannot find is left blank, not zeroed: "no entry in GameLog" and
    # "no trophies earned" are different claims and 0/0 would assert the second.
    if progress:
        index = {_np_title_key(v.get("title", "")): v for v in progress.values()}
        for r in rows:
            # Console rows arrive by two different routes and do NOT share a kind:
            # Xbox is built here as kind="game", while PlayStation comes through Home
            # Assistant's playstation_network integration as an ordinary media_player,
            # so it is kind="media" like a Sonos. Match on the device instead, or the
            # PS5 silently gets no progress line -- which is exactly what happened.
            where = str(r.get("where") or "").lower()
            is_console = (r.get("kind") == "game"
                          or "playstation" in where or "xbox" in where)
            if not is_console:
                continue
            hit = index.get(_np_title_key(r.get("what", "")))
            if not hit or not hit.get("t"):
                continue
            # Structured rather than a prebuilt string: PlayStation gets Sony's own
            # four-tier trophy breakdown drawn as glyphs, Xbox gets a plain count,
            # and the template decides how each looks.
            r["prog"] = {
                "word": "trophies" if hit.get("service") == "psn" else "achievements",
                "e": hit.get("e") or 0,
                "t": hit["t"],
                "pct": hit.get("pct"),
                "hours": f"{hit['m'] / 60:.0f}h" if hit.get("m") else None,
                # Present only for titles that really have trophies -- see the export.
                "tro": hit.get("tro"),
                # Xbox's equivalent of the trophy tiers: gamerscore earned/total,
                # shown the way the Xbox UI shows it.
                "gs": hit.get("gs"),
            }

    if not rows:
        p.headline = "Nothing playing"
        p.sub = "Idle devices are hidden — this lists only what's actually in use."
    else:
        playing = [r for r in rows if r["state"] != "paused"]
        p.headline = (f"{rows[0]['what']} · {rows[0]['where']}" if len(rows) == 1
                      else f"{len(rows)} playing — {rows[0]['what']} · {rows[0]['where']}")
        if not playing:
            p.sub = "All paused, recently — nothing actively playing."
    return p
