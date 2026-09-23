"""Tests for the parts that are easy to get subtly wrong.

Run: .venv/bin/python -m pytest -q     (or: python3 tests/test_logic.py)

Deliberately no network: every panel builder is a pure function of collected
data, which is the whole reason they were written that way.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.liveness import build_last_seen, device_key           # noqa: E402
from app.model import Reading, Status, worst                    # noqa: E402
from app.panels import (                                         # noqa: E402
    security_panel, trash_banner, circuit_trend, collection_panel,
    RECENT_WINDOW, TREND_MIN_PCT, TREND_MIN_WATTS,
)
from app.db import Database                                      # noqa: E402
from app.sources import parse_netscape_bookmarks                # noqa: E402
from app.trash_schedule import build_payload, recycling_type    # noqa: E402


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


# --- liveness ---------------------------------------------------------------
def test_device_key_collapses_channels_and_shelly_duplication():
    assert device_key("sensor.sump_pump_power") == "sump_pump"
    assert device_key("sensor.sump_pump_voltage") == "sump_pump"
    assert device_key("sensor.basementfridge_basementfridge_power") == "basementfridge"
    assert device_key("binary_sensor.laundry_leak_moisture") == "laundry_leak"


def test_device_liveness_uses_the_freshest_channel():
    """The real 2026-08-20 case: constant 0 W power, but voltage moving.

    A per-entity check would call the sump dead. The device-level max must not.
    """
    now = time.time()
    states = {
        "sensor.sump_pump_power": {"state": "0", "last_updated": iso(now - 4000)},
        "sensor.sump_pump_voltage": {"state": "117.4", "last_updated": iso(now - 30)},
    }
    seen = build_last_seen(states)
    assert now - seen["sump_pump"] < 60


def test_automations_do_not_count_as_device_liveness():
    now = time.time()
    states = {
        "automation.sump_pump_power_lost": {"state": "on", "last_updated": iso(now)},
        "sensor.sump_pump_power": {"state": "0", "last_updated": iso(now - 90000)},
    }
    seen = build_last_seen(states)
    assert now - seen["sump_pump"] > 80000, "an automation must not vouch for a device"


# --- status algebra ---------------------------------------------------------
def test_unknown_outranks_ok_but_not_alert():
    assert worst(Status.OK, Status.UNKNOWN) is Status.UNKNOWN
    assert worst(Status.UNKNOWN, Status.ALERT) is Status.ALERT
    assert worst(Status.OK, Status.OK) is Status.OK


def test_stale_reading_is_never_reported_as_ok():
    r = Reading("Sump", "Dry", as_of=time.time() - 10_000, max_age=3600, status=Status.OK)
    assert r.stale
    assert r.effective_status is Status.UNKNOWN, "staleness must beat the caller's optimism"


def test_missing_value_is_unknown_not_ok():
    assert Reading("X", None, status=Status.OK).effective_status is Status.UNKNOWN
    assert Reading("X", "unavailable", status=Status.OK).effective_status is Status.UNKNOWN


# --- leak panel -------------------------------------------------------------
def test_wet_sensor_raises_alert():
    now = time.time()
    states = {
        "binary_sensor.laundry_leak_moisture": {"state": "on", "last_updated": iso(now)},
        "sensor.sump_pump_voltage": {"state": "117", "last_updated": iso(now)},
    }
    p = security_panel(states)
    assert p.rollup() is Status.ALERT
    assert "WATER DETECTED" in p.headline


def test_silent_leak_sensor_does_not_read_as_dry():
    """The failure this project exists to catch."""
    now = time.time()
    states = {
        "binary_sensor.laundry_leak_moisture": {"state": "off", "last_updated": iso(now - 900_000)},
        "sensor.sump_pump_voltage": {"state": "117", "last_updated": iso(now)},
    }
    p = security_panel(states)
    assert p.rollup() is Status.UNKNOWN
    assert "not reporting" in p.headline


def test_sump_losing_supply_is_an_alert():
    now = time.time()
    states = {"sensor.sump_pump_voltage": {"state": "0", "last_updated": iso(now)}}
    p = security_panel(states)
    assert p.rollup() is Status.ALERT


# --- trash ------------------------------------------------------------------
def test_recycling_alternates_weekly_from_the_anchor():
    anchor = dt.date(2025, 7, 9)
    assert recycling_type(anchor) == "paper"
    assert recycling_type(anchor + dt.timedelta(days=7)) == "containers"
    assert recycling_type(anchor + dt.timedelta(days=14)) == "paper"


def test_holiday_in_pickup_week_shifts_to_thursday():
    # Independence Day 2026 falls on a Saturday, so no shift that week.
    # Christmas 2026 is a Friday - also no shift. Use New Year's Day 2027 (Friday).
    # Labor Day 2026 = Mon Sep 7 -> that week's Wednesday pickup shifts.
    payload = build_payload(dt.date(2026, 9, 7))
    assert payload["shifted"] is True
    assert payload["pickup_day"] == "Thursday"
    assert payload["shift_reason"] == "Labor Day"


def test_the_banner_is_absent_until_the_day_before_pickup():
    """It replaced a card that sat there all week saying nothing actionable."""
    import app.panels as P
    real = P.build_payload
    def fake(days):
        return lambda d: {"pickup_date": (d + dt.timedelta(days=days)).isoformat(),
                          "pickup_day": "Wednesday", "type": "Paper & Cardboard",
                          "shifted": False, "shift_reason": "", "note": ""}
    try:
        for days in (2, 3, 5, 6):
            P.build_payload = fake(days)
            assert trash_banner() is None, f"{days} days out should show nothing"
        P.build_payload = fake(1)
        b = trash_banner()
        assert b and b["today"] is False and b["type"] == "Paper & Cardboard"
        P.build_payload = fake(0)
        assert trash_banner()["today"] is True
    finally:
        P.build_payload = real


def test_the_banner_follows_the_pickup_date_not_a_hardcoded_tuesday():
    """Normal collection is Wednesday, so the reminder lands on Tuesday -- but holiday
    weeks slide to Thursday, and a fixed Tuesday would be wrong exactly then."""
    from app.trash_schedule import build_payload as real_payload
    # Labor Day week 2026: pickup moves to Thursday Sep 10.
    assert real_payload(dt.date(2026, 9, 7))["pickup_day"] == "Thursday"
    assert trash_banner(dt.date(2026, 9, 8)) is None      # Tuesday: nothing yet
    b = trash_banner(dt.date(2026, 9, 9))                 # Wednesday: eve of pickup
    assert b and b["day"] == "Thursday" and b["shifted"] is True


# --- bookmark import --------------------------------------------------------
def test_netscape_import_extracts_urls_and_folders():
    html = """<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p>
      <DT><H3>Homelab</H3>
      <DL><p>
        <DT><A HREF="https://pve1.example.com">Proxmox</A>
        <DT><A HREF="https://ha.example.com">Home Assistant</A>
      </DL><p>
      <DT><A HREF="https://example.com">Loose</A>
    </DL><p>"""
    marks = parse_netscape_bookmarks(html)
    urls = {m["url"] for m in marks}
    assert "https://pve1.example.com" in urls
    assert len(marks) == 3
    assert any(m["folder"] == "Homelab" for m in marks)


def test_netscape_import_skips_javascript_and_dedupes():
    html = """<DL><p>
      <DT><A HREF="javascript:void(0)">Bad</A>
      <DT><A HREF="https://a.test">A</A>
      <DT><A HREF="https://a.test">A again</A>
    </DL><p>"""
    marks = parse_netscape_bookmarks(html)
    assert len(marks) == 1 and marks[0]["url"] == "https://a.test"


# --- trash: a provenance caveat is not an operational warning ------------------
def test_unverified_schedule_does_not_make_the_banner_warn():
    """It read WARN purely because the schedule anchor had aged out -- true every day
    once that happens, i.e. a permanent false alarm. The caveat is a footnote."""
    import app.panels as P
    real = P.build_payload
    P.build_payload = lambda d: {"pickup_date": (d + dt.timedelta(days=1)).isoformat(),
                                 "pickup_day": "Wednesday", "type": "Paper & Cardboard",
                                 "shifted": False, "shift_reason": "",
                                 "note": "Schedule beyond 2026-06-30 unverified"}
    try:
        b = trash_banner()
        assert b["status"] == "info", f"got {b['status']}"
        assert "unverified" in b["note"]
    finally:
        P.build_payload = real


def test_a_changed_source_calendar_still_warns():
    """That one genuinely means: go and look."""
    import app.panels as P
    real = P.build_payload
    P.build_payload = lambda d: {"pickup_date": (d + dt.timedelta(days=1)).isoformat(),
                                 "pickup_day": "Wednesday", "type": "Trash",
                                 "shifted": False, "shift_reason": "",
                                 "note": "Source calendar changed — verify schedule"}
    try:
        assert trash_banner()["status"] == "warn"
    finally:
        P.build_payload = real


# --- hvac liveness: per device, and battery sensors are slow -------------------
def test_quiet_sensor_on_a_live_device_is_not_reported_dead():
    """A room sensor that has not changed value looks stale by its own timestamp while its
    device is demonstrably alive. Uses a standalone sensor, not a thermostat's own — those
    are now suppressed from the readings because the zone table already shows them."""
    from app.panels import hvac_panel
    now = time.time()
    fresh = dt.datetime.fromtimestamp(now - 30, dt.timezone.utc).isoformat()
    old = dt.datetime.fromtimestamp(now - 5 * 3600, dt.timezone.utc).isoformat()
    p = hvac_panel({
        "sensor.hallway_temperature": {"state": "68.1", "attributes": {
            "device_class": "temperature", "friendly_name": "Hallway"},
            "last_updated": old},
        "sensor.hallway_humidity": {"state": "41", "attributes": {
            "device_class": "humidity"}, "last_updated": fresh},
    })
    r = next(r for r in p.readings if r.unit == "°F")
    assert not r.stale, "device-level liveness should rescue a quiet entity"


def test_every_room_with_a_temperature_is_listed():
    """Originally the fix for duplication was to drop zone rooms from the readings, which
    also dropped them from the card entirely. Every room should be listed; the TABLE gave
    up its duplicate "Now" column instead."""
    from app.panels import hvac_panel
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    p = hvac_panel({
        "climate.primary_en_suite": {"state": "heat", "attributes": {
            "hvac_action": "idle", "current_temperature": 67.5, "temperature": 50.0,
            "friendly_name": "Primary En Suite"}, "last_updated": now},
        "sensor.primary_en_suite_temperature": {"state": "67.3", "attributes": {
            "device_class": "temperature", "friendly_name": "Primary En Suite"},
            "last_updated": now},
        "sensor.robins_bedroom_temperature": {"state": "72.1", "attributes": {
            "device_class": "temperature", "friendly_name": "Robins Bedroom"},
            "last_updated": now},
    })
    labels = [r.label for r in p.readings if r.unit == "°F"]
    assert len(labels) == 2, labels
    assert len(p.rows) == 1


def test_a_chip_temperature_is_not_a_room():
    """The Zigbee coordinator reports 105°F core temp; selecting on device_class alone
    would have listed it as a room."""
    from app.panels import hvac_panel
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    p = hvac_panel({"sensor.slzb_06u_core_chip_temp": {"state": "105.4", "attributes": {
        "device_class": "temperature", "friendly_name": "SLZB Core chip temp"},
        "last_updated": now}})
    assert not [r for r in p.readings if r.unit == "°F"]


def test_action_is_derived_from_power_when_the_integration_omits_it():
    """The Samsung minisplits expose no hvac_action, so the card could only say unknown.
    A compressor either draws current or it does not."""
    from app.panels import hvac_panel
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    def zone(watts):
        return hvac_panel({
            "climate.bedroom_minisplit": {"state": "cool", "attributes": {
                "current_temperature": 72, "temperature": 68,
                "friendly_name": "Bedroom Minisplit"}, "last_updated": now},
            "sensor.bedroom_minisplit_power": {"state": str(watts), "attributes": {
                "device_class": "power"}, "last_updated": now},
        }).rows[0]["action"]
    # ⚠️ REWRITTEN 2026-09-07. This asserted zone(0) == "idle", i.e. that a LOW power
    # reading proves the unit is not running. It does not: the sensor emits ~9 samples
    # in 6 hours and never exceeded 50 W on these units, so a compressor cycle between
    # samples is invisible. The room here is 72 against a 68 cool setpoint -- the unit
    # SHOULD be cooling, and calling that "idle" on the strength of one stale sample is
    # exactly the false reassurance this dashboard exists to avoid.
    # Power is now positive evidence only: high proves running, low proves nothing.
    assert zone(0) == "should be cooling", "a low sample must not prove idle"
    assert zone(2) == "should be cooling"
    assert zone(430) == "cooling"


def test_action_stays_unknown_with_no_signal_at_all():
    """No signal at all is still unknown — the point is to observe, not to assume.

    Narrowed 2026-09-07: a missing power sensor is no longer "no signal". Mode,
    setpoint and room temperature are reliable and say plenty. Genuinely no signal
    means no setpoint AND no convincing power reading."""
    from app.panels import hvac_panel
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    useful = hvac_panel({"climate.x": {"state": "cool", "attributes": {
        "current_temperature": 72, "temperature": 68, "friendly_name": "X"},
        "last_updated": now}})
    assert useful.rows[0]["action"] == "should be cooling"
    blind = hvac_panel({"climate.y": {"state": "cool", "attributes": {
        "current_temperature": 72, "friendly_name": "Y"}, "last_updated": now}})
    assert blind.rows[0]["action"] == "unknown", "no setpoint, no power -> no claim"

def test_a_battery_sensor_gets_a_longer_leash():
    """A battery room sensor reporting every ~2.5h is healthy, not missing."""
    from app.panels import hvac_panel
    now = time.time()
    t = dt.datetime.fromtimestamp(now - int(2.5 * 3600), dt.timezone.utc).isoformat()
    states = {
        "sensor.robins_bedroom_temperature": {"state": "71.8", "attributes": {
            "device_class": "temperature", "friendly_name": "Robins Bedroom"},
            "last_updated": t},
        "sensor.robins_bedroom_battery": {"state": "100", "attributes": {
            "device_class": "battery"}, "last_updated": t},
    }
    r = next(r for r in hvac_panel(states).readings if r.unit == "°F")
    assert not r.stale, "2.5h on a battery sensor must not read as not-reporting"


def test_a_genuinely_dead_battery_sensor_still_trips():
    from app.panels import hvac_panel
    now = time.time()
    t = dt.datetime.fromtimestamp(now - 20 * 3600, dt.timezone.utc).isoformat()
    states = {
        "sensor.robins_bedroom_temperature": {"state": "71.8", "attributes": {
            "device_class": "temperature", "friendly_name": "Robins Bedroom"},
            "last_updated": t},
        "sensor.robins_bedroom_battery": {"state": "100", "attributes": {
            "device_class": "battery"}, "last_updated": t},
    }
    r = next(r for r in hvac_panel(states).readings if r.unit == "°F")
    assert r.stale, "20h silent IS dead, even for a battery sensor"


# --- hvac panel: mode is not action -------------------------------------------
def _clim(state, action, cur, target, upd="2026-08-28T02:00:00+00:00"):
    a = {"current_temperature": cur, "temperature": target, "friendly_name": "Zone"}
    if action is not None:
        a["hvac_action"] = action
    return {"state": state, "attributes": a, "last_updated": upd}


def test_a_zone_that_is_off_is_not_a_blind_spot():
    """2026-09-12: both minisplits switched off in a 66F room set to 70, and the card said
    "Can't tell if anything is running — 2 zones not reporting heat/cool state". Off is a
    KNOWN state. Only a unit that is on and will not say what it is doing is a blind spot."""
    from app.panels import hvac_panel
    p = hvac_panel({"climate.bedroom_minisplit": _clim("off", None, 66, 70),
                    "climate.bathroom_minisplit": _clim("off", None, 66, 70)})
    assert [r["action"] for r in p.rows] == ["off", "off"], p.rows
    assert "not reporting" not in p.headline, p.headline
    assert "Can't tell" not in p.headline, p.headline


def test_zone_with_no_reported_action_is_unknown_not_its_mode():
    """The real bug: a minisplit set to `cool` whose integration reports no hvac_action
    rendered as 'doing: cool' — while the headline, reading actions properly, said nothing
    was calling. Table and headline disagreed from identical data."""
    from app.panels import hvac_panel
    p = hvac_panel({"climate.minisplit": _clim("cool", None, 66, 70)})
    row = p.rows[0]
    assert row["mode"] == "cool"
    # The rule is that `action` must never be the MODE echoed back -- that was the
    # original bug. It may be something DERIVED from mode + setpoint + room temp, which
    # is what "satisfied" is: 66 in a room set to cool at 70 has nothing to do.
    assert row["action"] != row["mode"], "action must not echo the mode"
    assert row["action"] == "satisfied", row


def test_a_bare_idle_headline_never_hides_a_mode_mismatch():
    """Renamed and rewritten 2026-09-07 (was ...does_not_claim_idle_when_a_zone_will_not_say).

    Its premise no longer holds: this zone -- cool mode, 70 setpoint, 66 room -- used to
    be a blind spot because nothing reported an action, and the rule was "never say
    Idle". The state is now DERIVED from mode + setpoint + room temperature, so Idle is
    correct here. What must never happen is a BARE "Idle — nothing calling for heat or
    cool", which would hide the reason the room will never reach 70.

    The genuine can't-tell case is covered by
    test_an_unreporting_zone_headline_leads_with_the_blind_spot."""
    from app.panels import hvac_panel
    p = hvac_panel({"climate.minisplit": _clim("cool", None, 66, 70)})
    assert p.headline != "Idle — nothing calling for heat or cool", \
        "a bare Idle would hide the mode mismatch"
    assert "already past the setpoint" in p.headline, p.headline


def test_genuinely_idle_zones_still_read_idle():
    from app.panels import hvac_panel
    p = hvac_panel({"climate.a": _clim("heat", "idle", 70, 68)})
    assert p.headline == "Idle — nothing calling for heat or cool"


def test_an_actively_running_zone_is_still_named():
    from app.panels import hvac_panel
    p = hvac_panel({"climate.a": _clim("heat", "heating", 64, 70)})
    assert "heating" in p.headline


def test_running_zone_wins_over_an_unknown_one():
    """A real call for heat is more important than a blind spot; report the call."""
    from app.panels import hvac_panel
    p = hvac_panel({"climate.a": _clim("heat", "heating", 64, 70),
                    "climate.b": _clim("cool", None, 66, 70)})
    assert "heating" in p.headline


# --- homelab panel: honesty about what it can and cannot see -----------------
def _pve_ok():
    return {
        "nodes": [{"node": "pve", "status": "online"}],
        "guests": [{"vmid": 100, "name": "x", "status": "running"}],
        "apt": {},
        "backups": {"pve": {"status": "OK", "starttime": time.time() - 3600}},
    }


def test_homelab_never_claims_all_clear_without_a_patch_report():
    from app.panels import homelab_panel
    p = homelab_panel(_pve_ok(), {}, 60, patch=None)
    assert p.rollup() is Status.UNKNOWN
    assert "No open exposure" not in p.headline, "must not claim an all-clear it can't support"
    assert "Cannot fully assess" in p.headline


def test_homelab_all_clear_needs_a_fresh_patch_report():
    from app.panels import homelab_panel
    patch = (time.time(), {"targets": [{"name": "pve", "count": 0, "security": 0}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=patch)
    assert "No open exposure" in p.headline


def test_homelab_treats_an_old_patch_report_as_unknown():
    """A report from last week is not evidence about today."""
    from app.panels import homelab_panel
    patch = (time.time() - 7 * 86400, {"targets": [{"name": "pve", "count": 0}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=patch)
    assert p.rollup() is Status.UNKNOWN
    assert "old" in " ".join(r.note for r in p.readings)


# --- homelab panel: self-hosted app versions ---------------------------------
def _fresh_patch():
    return (time.time(), {"targets": [{"name": "pve", "count": 0, "security": 0}]})


def test_apps_absent_adds_no_reading_at_all():
    """Not deployed is not the same as unobservable.

    Before sagamore-app-push exists on pve we have made no claim about apps, so
    there is nothing to render. Once it HAS reported, staleness must degrade to
    unknown (the test below) — that is the case the honesty rule is about.
    """
    from app.panels import homelab_panel
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=None)
    assert not any(r.label == "Self-hosted apps" for r in p.readings)
    assert "No open exposure" in p.headline


def test_apps_all_current_reads_ok():
    from app.panels import homelab_panel
    apps = (time.time(), {"apps": [{"name": "sonarr", "state": "ok"},
                                   {"name": "plex", "state": "ok"}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=apps)
    r = next(r for r in p.readings if r.label == "Self-hosted apps")
    assert r.effective_status is Status.OK
    assert r.value == "all 2 current"
    assert len(p.app_rows) == 2


def test_apps_pending_update_warns_and_names_them():
    from app.panels import homelab_panel
    apps = (time.time(), {"apps": [{"name": "sonarr", "state": "ok"},
                                   {"name": "searxng", "state": "update"}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=apps)
    r = next(r for r in p.readings if r.label == "Self-hosted apps")
    assert r.effective_status is Status.WARN
    assert "searxng" in r.note
    assert "No open exposure" not in p.headline


def test_apps_failed_update_is_an_alert_not_a_warning():
    """A rolled-back or unhealthy app needs a human tonight, not on Monday."""
    from app.panels import homelab_panel
    apps = (time.time(), {"apps": [{"name": "metube", "state": "failed"},
                                   {"name": "searxng", "state": "update"}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=apps)
    r = next(r for r in p.readings if r.label == "Self-hosted apps")
    assert r.effective_status is Status.ALERT
    assert "metube" in r.note


def test_apps_worst_state_sorts_first():
    from app.panels import homelab_panel
    apps = (time.time(), {"apps": [{"name": "aaa", "state": "ok"},
                                   {"name": "zzz", "state": "failed"},
                                   {"name": "mmm", "state": "update"}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=apps)
    assert [r["name"] for r in p.app_rows] == ["zzz", "mmm", "aaa"]


def test_apps_stale_report_degrades_to_unknown():
    from app.panels import homelab_panel
    apps = (time.time() - 7 * 86400, {"apps": [{"name": "sonarr", "state": "ok"}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=apps)
    r = next(r for r in p.readings if r.label == "Self-hosted apps")
    assert r.effective_status is Status.UNKNOWN
    assert "Cannot fully assess" in p.headline


def test_apps_empty_list_is_unknown_not_all_clear():
    """An empty report is a failed collector, not eleven healthy apps.

    The pusher refuses to post an empty list for exactly this reason; this is
    the second line of defence if one ever gets through.
    """
    from app.panels import homelab_panel
    apps = (time.time(), {"apps": []})
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=apps)
    r = next(r for r in p.readings if r.label == "Self-hosted apps")
    assert r.effective_status is Status.UNKNOWN
    assert r.value is None


def test_apps_unreadable_app_never_counts_as_current():
    from app.panels import homelab_panel
    apps = (time.time(), {"apps": [{"name": "paperless", "state": "unknown"},
                                   {"name": "sonarr", "state": "ok"}]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(), apps=apps)
    r = next(r for r in p.readings if r.label == "Self-hosted apps")
    assert r.effective_status is Status.UNKNOWN
    assert "1 unreadable" in r.note


def test_homelab_reports_host_and_container_updates_separately():
    """Renamed and rewritten 2026-09-10 (was ..._counts_..._across_hosts_and_containers).

    It asserted the combined total, 8. That summing is exactly what produced a warning
    every morning about three hypervisors nothing is allowed to touch: guests are cleared
    by the 05:00 routine run, hosts are Tier H and need a planned rolling reboot. Both
    numbers are still reported and nothing is hidden -- they are just no longer added
    together, because they do not mean the same thing."""
    from app.panels import homelab_panel
    patch = (time.time(), {"targets": [
        {"name": "pve", "count": 3, "security": 1, "kind": "host"},
        {"name": "ct121", "count": 5, "security": 0, "reboot": True, "kind": "guest"},
    ]})
    p = homelab_panel(_pve_ok(), {}, 60, patch=patch)
    pending = [r for r in p.readings if r.label == "Pending updates"][0]
    assert pending.value == 5, "the guest count alone"
    assert "reboot needed" in pending.note
    host = [r for r in p.readings if r.label == "Host updates"][0]
    assert host.value == 3 and "1 security" in host.note, host.note
    assert host.informational, "hosts are scheduled work, not a fault"


# --- consoles ---------------------------------------------------------------
def _leak_states(sensor_age_h, hb_age_s, wet=False):
    now = time.time()
    return {
        "binary_sensor.laundry_leak_moisture": {
            "state": "on" if wet else "off",
            "last_updated": iso(now - sensor_age_h * 3600), "attributes": {}},
        "sensor.sump_pump_voltage": {"state": "117", "last_updated": iso(now), "attributes": {}},
        "alarm_control_panel.alarm_control_panel": {
            "state": "armed_away", "last_updated": iso(now - hb_age_s), "attributes": {}},
    }


def test_quiet_leak_sensor_is_ok_while_its_hub_checks_in():
    """The 2026-08-21 false alarm: 16h-silent SimpliSafe sensors, healthy hub."""
    p = security_panel(_leak_states(sensor_age_h=16, hb_age_s=36))
    assert p.rollup() is Status.OK, "an event-driven sensor is not dead just because it is quiet"
    assert "everything dry" in p.headline


def test_quiet_leak_sensor_is_unknown_when_the_hub_goes_dark():
    """If the hub stops polling, we genuinely cannot vouch for the sensors."""
    p = security_panel(_leak_states(sensor_age_h=16, hb_age_s=6 * 3600))
    assert p.rollup() is Status.ALERT
    assert "unverifiable" in p.headline


def test_wet_still_alerts_even_with_a_dead_hub():
    p = security_panel(_leak_states(sensor_age_h=1, hb_age_s=6 * 3600, wet=True))
    assert p.rollup() is Status.ALERT
    assert "WATER DETECTED" in p.headline


# --- labelling: neither name source is reliable alone ------------------------
def test_friendly_name_picks_the_better_of_two_bad_sources():
    from app.panels import _friendly
    states = {
        # friendly_name is a labelling slip - the id is the real name
        "sensor.basement_pool_table_power": {"attributes": {"friendly_name": "Power"}},
        # friendly_name duplicates itself - shorter id wins
        "sensor.gym_light_power": {"attributes": {"friendly_name": "Gym Light gymLight power"}},
        # id is an opaque hex blob - friendly_name wins
        "cover.athom_garage_door_00dd68_garage_door": {
            "attributes": {"friendly_name": "Sam's Garage Door"}},
        # apostrophe cannot be expressed in an entity id - friendly_name wins
        "cover.alex_s_garage_door": {"attributes": {"friendly_name": "Alex's Garage Door"}},
    }
    assert _friendly("sensor.basement_pool_table_power", states) == "Basement Pool Table"
    assert _friendly("sensor.gym_light_power", states) == "Gym Light"
    assert _friendly("cover.athom_garage_door_00dd68_garage_door", states) == "Sam's Garage Door"
    assert _friendly("cover.alex_s_garage_door", states) == "Alex's Garage Door"


# --- power: the unmetered remainder --------------------------------------
def _power_states(watts_by_name, forecast_kwh=1622, typical=1344):
    now = time.time()
    st = {}
    for name, w in watts_by_name.items():
        st[f"sensor.{name}_power"] = {
            "state": str(w), "last_updated": iso(now),
            "attributes": {"device_class": "power", "unit_of_measurement": "W",
                           "friendly_name": name}}
    for key, val in (("current_bill_electric_forecasted_usage", forecast_kwh),
                     ("typical_monthly_electric_usage", typical)):
        st[f"sensor.national_grid_{key}"] = {"state": str(val), "last_updated": iso(now),
                                             "attributes": {}}
    return st


def test_power_names_the_unmetered_remainder():
    """The rack must not look like the whole house."""
    from app.config import Config
    from app.panels import power_panel
    p = power_panel(_power_states({"networkrack": 294, "dishwasher": 1}), Config())
    labels = {r.label: r for r in p.readings}
    assert "Unidentified" in labels, "unmetered load must be shown, not implied"
    house = float(str(labels["Whole house"].value).replace(",", ""))
    meas = float(str(labels["Measured circuits"].value).replace(",", ""))
    unid = float(str(labels["Unidentified"].value).replace(",", ""))
    assert abs((meas + unid) - house) <= 1, "measured + unidentified must equal the house"
    assert unid > meas, "with 12 plugs the unmetered share should dominate"


def test_power_sub_line_puts_the_biggest_circuit_in_proportion():
    from app.config import Config
    from app.panels import power_panel
    p = power_panel(_power_states({"networkrack": 294}), Config())
    assert "% of the house" in p.sub


def test_power_without_utility_data_does_not_invent_a_denominator():
    from app.config import Config
    from app.panels import power_panel
    st = _power_states({"networkrack": 294})
    del st["sensor.national_grid_current_bill_electric_forecasted_usage"]
    p = power_panel(st, Config())
    assert not any(r.label == "Unidentified" for r in p.readings)


# --- consoles: PlayStation firmware IS readable ------------------------------
def test_ps_version_decoding():
    from app.sources import decode_ps_version
    assert decode_ps_version("13600007") == "13.60"
    assert decode_ps_version("09000000") == "9.00"
    assert decode_ps_version("") == ""


def test_informational_reading_is_documented_fact_not_blind_spot():
    """PS5 storage is unobtainable; that is knowledge, not missing data."""
    from app.model import Panel
    p = Panel(key="x", title="X", readings=[
        Reading("PS5 storage", "not exposed", informational=True),
        Reading("PS5", "Rest mode", as_of=time.time()),
    ])
    assert p.rollup() is Status.OK, "a documented-unobtainable value must not read as UNKNOWN"


def test_informational_flag_does_not_mask_a_real_blind_spot():
    from app.model import Panel
    p = Panel(key="x", title="X", readings=[
        Reading("PS5 storage", "not exposed", informational=True),
        Reading("Something real", None),          # genuinely missing
    ])
    assert p.rollup() is Status.UNKNOWN


def _mp(state, **attrs):
    return {"state": state, "last_changed": iso(time.time()),
            "attributes": {"friendly_name": attrs.pop("name", "Speaker"), **attrs}}


def test_html_entities_from_station_metadata_are_decoded():
    """2026-09-12: the card showed "Jay-Z &amp; Lil". HOT 96.9's stream metadata arrives
    from HA already HTML-encoded (media_artist = 'Fat Joe f/ Ashanti &amp; Ja Rule',
    media_title = "What&apos;s Luv"), and the template escapes it again. Decode once here
    -- NOT by marking the string safe in the template, which would hand whatever a radio
    station puts in its metadata straight into the page."""
    from app.panels import now_playing_panel
    states = {"media_player.primary_en_suite": _mp(
        "playing", name="Primary En Suite",
        media_title="What&apos;s Luv", media_artist="Fat Joe f/ Ashanti &amp; Ja Rule")}
    p = now_playing_panel(states)
    what = p.rows[0]["what"]
    assert "&amp;" not in what and "&apos;" not in what, what
    assert what == "What's Luv — Fat Joe f/ Ashanti & Ja Rule", what


def test_a_bare_ampersand_in_a_title_survives_decoding():
    """`& dj friz` is already correct (Kitchen Home Pod, same day) -- decoding must not
    mangle a title that was never encoded, and must not double-decode."""
    from app.panels import now_playing_panel
    states = {"media_player.kitchen": _mp(
        "playing", name="Kitchen", media_title="And July (feat. DEAN & dj friz)",
        media_artist="Heize")}
    assert now_playing_panel(states).rows[0]["what"] == \
        "And July (feat. DEAN & dj friz) — Heize"


def test_sonos_group_collapses_to_one_row():
    """Four speakers on one track is one thing happening, not four."""
    from app.panels import now_playing_panel
    grp = ["media_player.basement_2", "media_player.downstairs_bathroom",
           "media_player.primary_en_suite"]
    states = {e: _mp("playing", name=e.split(".")[1].title(),
                     media_title="Friday", media_artist="Rebecca Black",
                     group_members=grp) for e in grp}
    p = now_playing_panel(states)
    assert len(p.rows) == 1, f"expected one grouped row, got {len(p.rows)}"
    assert "+2" in p.rows[0]["where"]
    assert p.rows[0]["what"] == "Friday — Rebecca Black"


def test_long_paused_device_is_not_in_use():
    """The real 2026-08-21 case: Sonos paused since yesterday's HA restart."""
    from app.panels import now_playing_panel
    stale = {"media_player.kitchen": _mp(
        "paused", name="Kitchen", media_title="It's Gonna Be Me",
        media_artist="*NSYNC",
        media_position_updated_at=iso(time.time() - 18 * 3600))}
    p = now_playing_panel(stale)
    assert p.rows == []
    assert p.headline == "Nothing playing"


def test_recently_paused_device_still_counts():
    from app.panels import now_playing_panel
    fresh = {"media_player.kitchen": _mp(
        "paused", name="Kitchen", media_title="It's Gonna Be Me",
        media_artist="*NSYNC",
        media_position_updated_at=iso(time.time() - 300))}
    p = now_playing_panel(fresh)
    assert len(p.rows) == 1
    assert "paused" in p.readings[0].note


def test_a_pause_outlives_ha_restamping_its_timestamps():
    """2026-09-12: HA restarted at 14:49:53 and restamped all 2112 entities, so the Office
    Apple TV -- paused since 05:03 the previous morning -- looked 15 minutes old and held
    the card for a day and a half. Sagamore's own record of when the pause began decides."""
    from app.panels import now_playing_panel
    states = {"media_player.office": _mp(
        "paused", name="Office", media_title="S6 - E21",
        media_position_updated_at=iso(time.time() - 300))}   # HA says 5 minutes old
    p = now_playing_panel(states, pause_since={
        "media_player.office": {"ident": "S6 - E21|9", "since": time.time() - 30 * 3600}})
    assert p.rows == [], "a 30-hour pause is not 'now playing'"
    assert p.headline == "Nothing playing"


def test_ha_timestamps_still_date_a_pause_we_have_not_recorded_yet():
    """First render after a deploy: no record of our own, so HA's clock is all there is."""
    from app.panels import now_playing_panel
    states = {"media_player.office": _mp(
        "paused", name="Office", media_title="S6 - E21",
        media_position_updated_at=iso(time.time() - 300))}
    assert len(now_playing_panel(states, pause_since={}).rows) == 1


def test_pausing_something_new_restarts_the_pause_clock():
    from app.updates import carry_pause_since
    prev = {"media_player.office": {"ident": "S6 E21|9", "since": 100.0}}
    same = carry_pause_since(prev, {"media_player.office": "S6 E21|9"}, 500.0)
    assert same["media_player.office"]["since"] == 100.0, "same frozen frame keeps its clock"
    new = carry_pause_since(prev, {"media_player.office": "S6 E22|0"}, 500.0)
    assert new["media_player.office"]["since"] == 500.0, "a different pause is new activity"
    # A player that stops reporting the pause is REMEMBERED, not forgotten -- see
    # test_a_pause_survives_the_device_being_turned_off_and_woken below.
    kept = carry_pause_since(prev, {}, 500.0)
    assert kept["media_player.office"]["since"] == 100.0, kept


def test_a_pause_survives_the_device_being_turned_off_and_woken():
    """The 2026-09-14 case: an HA automation turns the Office Apple TV on every morning and
    whenever the alarm is disarmed. It wakes showing the SAME frozen Plex episode (same
    title, same position, untouched since 09-11), and because the record was dropped while
    the TV was off, every wake read as a brand-new pause and took the card for 20 minutes."""
    from app.updates import carry_pause_since
    ident = "S6 - E21|9"
    day1 = carry_pause_since({}, {"media_player.office": ident}, 1000.0)
    off = carry_pause_since(day1, {}, 2000.0)                    # automation turns it off
    woken = carry_pause_since(off, {"media_player.office": ident}, 3000.0)   # and back on
    assert woken["media_player.office"]["since"] == 1000.0, "same frozen frame, same clock"


def test_resuming_and_pausing_elsewhere_starts_a_new_clock():
    """Anything a human actually did moves the position, so retention cannot hide real use."""
    from app.updates import carry_pause_since
    prev = carry_pause_since({}, {"media_player.office": "S6 - E21|9"}, 1000.0)
    off = carry_pause_since(prev, {}, 2000.0)
    later = carry_pause_since(off, {"media_player.office": "S6 - E21|1400"}, 3000.0)
    assert later["media_player.office"]["since"] == 3000.0, "watched on -> new pause"


def test_a_remembered_pause_is_pruned_after_a_week():
    from app.updates import carry_pause_since, RETAIN_PAUSE
    prev = {"media_player.office": {"ident": "x", "since": 100.0, "last_seen": 100.0}}
    assert carry_pause_since(prev, {}, 100.0 + RETAIN_PAUSE - 1) != {}, "still remembered"
    assert carry_pause_since(prev, {}, 100.0 + RETAIN_PAUSE + 1) == {}, "dropped after a week"


def test_a_paused_entry_is_stable_across_polls():
    """main.py writes the snapshot whenever the map changes; a timestamp that ticked every
    poll would mean a database write every poll."""
    from app.updates import carry_pause_since
    a = carry_pause_since({}, {"media_player.office": "x|9"}, 1000.0)
    b = carry_pause_since(a, {"media_player.office": "x|9"}, 1030.0)
    assert a == b, (a, b)


def test_an_unrecorded_pause_is_seeded_from_has_clock_not_from_now():
    """Otherwise the first poll after a deploy hands every standing pause a fresh grace
    period -- worse than the HA timestamp it replaced."""
    from app.updates import carry_pause_since
    out = carry_pause_since({}, {"media_player.office": "S6 E21|9"}, 1000.0,
                            {"media_player.office": 400.0})
    assert out["media_player.office"]["since"] == 400.0
    # No seed at all (HA has no timestamp either): now is all there is.
    assert carry_pause_since({}, {"media_player.x": "a"}, 1000.0)["media_player.x"]["since"] == 1000.0
    # A restamped clock must not push a pause into the future.
    assert carry_pause_since({}, {"media_player.x": "a"}, 1000.0,
                             {"media_player.x": 9999.0})["media_player.x"]["since"] == 1000.0


def test_idle_and_off_devices_are_hidden_entirely():
    from app.panels import now_playing_panel
    states = {f"media_player.d{i}": _mp(st)
              for i, st in enumerate(("off", "idle", "standby", "unavailable"))}
    p = now_playing_panel(states)
    assert p.rows == []


def test_tv_episode_reads_as_series_then_title():
    from app.panels import now_playing_panel
    states = {"media_player.living_room": _mp(
        "playing", name="Living Room Apple TV",
        media_series_title="Slow Horses", media_title="Footprints")}
    p = now_playing_panel(states)
    assert p.rows[0]["what"] == "Slow Horses — Footprints"


def test_the_local_playstation_probe_is_not_a_now_playing_source():
    """It used to be, alongside Home Assistant's playstation_network integration, and
    the PS5 duly appeared twice whenever it was mid-game. Consoles now come from HA
    like every other media player; the DDP probe survives only for firmware on the
    homelab card. Asserted by signature, because the failure mode is someone
    reintroducing the argument and assuming it does something."""
    import inspect
    from app.panels import now_playing_panel
    assert "playstations" not in inspect.signature(now_playing_panel).parameters



# --- per-circuit power trend ------------------------------------------------
# The interesting failures here are all false POSITIVES: a dashboard that cries
# "+300%" about a phone charger trains you to ignore it, which is worse than
# not having the feature.

def _seeded_db(tmpname, recent_w, base_w, *, base_hours=48.0, recent_pts=48,
               base_pts=96, now=None):
    """A Database with one circuit's history laid down at two flat levels."""
    import tempfile, os
    now = int(now or time.time())
    path = os.path.join(tempfile.mkdtemp(), tmpname)
    db = Database(path)
    key = "sensor.x_power"
    if recent_w is not None:
        for i in range(recent_pts):
            ts = now - int(RECENT_WINDOW * i / recent_pts)
            db.record({f"power:{key}": recent_w}, ts=ts)
    if base_w is not None:
        for i in range(base_pts):
            ts = now - RECENT_WINDOW - 60 - int(base_hours * 3600 * i / base_pts)
            db.record({f"power:{key}": base_w}, ts=ts)
    return db, key, now


def test_trend_learning_without_a_baseline():
    """No prior history means no opinion. This is the honest default."""
    db, key, now = _seeded_db("a.db", 100.0, None)
    assert circuit_trend(db, key, now)["state"] == "learning"
    assert circuit_trend(None, key, now)["state"] == "learning"


def test_trend_learning_when_baseline_too_short():
    """Two hours of history is not a fortnight and must not pretend to be."""
    db, key, now = _seeded_db("b.db", 100.0, 50.0, base_hours=2.0, base_pts=8)
    assert circuit_trend(db, key, now)["state"] == "learning"


def test_trend_detects_a_real_rise():
    db, key, now = _seeded_db("c.db", 120.0, 80.0)
    t = circuit_trend(db, key, now)
    assert t["state"] == "up"
    assert 45 < t["pct"] < 55           # +50%
    assert t["delta_w"] > 0


def test_trend_detects_a_real_fall():
    db, key, now = _seeded_db("d.db", 40.0, 100.0)
    t = circuit_trend(db, key, now)
    assert t["state"] == "down" and t["pct"] < 0


def test_small_load_moving_is_not_news():
    """8 W -> 11.5 W is +44%, well past the percent gate, but three and a half
    watts is nothing. The watt gate has to suppress it, or every trickle load
    in the house shouts every day."""
    db, key, now = _seeded_db("e.db", 11.5, 8.0)
    t = circuit_trend(db, key, now)
    assert t["pct"] > TREND_MIN_PCT              # percentage alone WOULD have fired
    assert abs(t["delta_w"]) < TREND_MIN_WATTS   # ...the watt gate stops it
    assert t["state"] == "flat"


def test_tiny_baseline_never_produces_a_percentage():
    """A 3 W charger going to 6 W must not print "+100%" - both because the
    change is trivial and because a percentage of ~nothing means nothing."""
    db, key, now = _seeded_db("e2.db", 6.0, 3.0)
    t = circuit_trend(db, key, now)
    assert t["pct"] is None
    assert t["state"] == "flat"


def test_large_load_wobbling_is_not_news():
    """+30 W on a 2 kW dryer is noise; the percent gate must suppress it."""
    db, key, now = _seeded_db("f.db", 2030.0, 2000.0)
    t = circuit_trend(db, key, now)
    assert abs(t["delta_w"]) > TREND_MIN_WATTS   # watts alone WOULD have fired
    assert abs(t["pct"]) < TREND_MIN_PCT
    assert t["state"] == "flat"


def test_trend_from_off_reports_watts_not_percent():
    """A circuit that was off all fortnight has no meaningful percentage;
    dividing by ~0 would print an absurd number instead of admitting that."""
    db, key, now = _seeded_db("g.db", 60.0, 0.0)
    t = circuit_trend(db, key, now)
    assert t["pct"] is None
    assert t["state"] == "up" and t["delta_w"] > TREND_MIN_WATTS


def test_idle_baseline_reports_watts_not_an_absurd_percentage():
    """Seen live on 2026-08-22: the dishwasher idles at ~1.1 W, ran once, and
    the panel offered "+4193%". Arithmetically right, informationally worthless.
    Anything whose baseline sits below the watt gate reports watts instead."""
    db, key, now = _seeded_db("i.db", 46.0, 1.1)
    t = circuit_trend(db, key, now)
    assert t["state"] == "up"
    assert t["pct"] is None, "a percentage against a 1.1 W baseline is noise"
    assert 44 < t["delta_w"] < 46


def test_trend_uses_the_mean_so_duty_cycling_does_not_fool_it():
    """A dishwasher is 0 W most of the day. Comparing instantaneous values, or
    a median, would call every cycle an infinite spike. Two windows with the
    same DUTY must read flat even though the samples are wildly different."""
    import tempfile, os
    now = int(time.time())
    db = Database(os.path.join(tempfile.mkdtemp(), "h.db"))
    key = "sensor.dish_power"
    for i in range(96):                       # baseline: 1 in 8 samples at 800 W
        ts = now - RECENT_WINDOW - 60 - int(48 * 3600 * i / 96)
        db.record({f"power:{key}": 800.0 if i % 8 == 0 else 0.0}, ts=ts)
    for i in range(48):                       # recent: same duty, offset phase
        ts = now - int(RECENT_WINDOW * i / 48)
        db.record({f"power:{key}": 800.0 if i % 8 == 3 else 0.0}, ts=ts)
    assert circuit_trend(db, key, now)["state"] == "flat"



def test_favicon_helper_has_its_dependencies():
    """_fetch_icon used httpx while main.py never imported it. Every request fell back to
    the placeholder globe, silently, because the NameError was swallowed into a debug log.
    A missing import inside a broadly-caught block is invisible at runtime — pin it."""
    import ast as _ast
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    tree = _ast.parse(src)
    imported = set()
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Import):
            imported.update(a.asname or a.name.split(".")[0] for a in n.names)
        elif isinstance(n, _ast.ImportFrom):
            imported.update(a.asname or a.name for a in n.names)
    for mod in ("httpx", "re", "time"):
        assert mod in imported, f"main.py uses {mod} but never imports it"



def test_href_regex_survives_a_data_uri_with_inner_quotes():
    """The inline SVG favicon uses single quotes inside a double-quoted href. A
    [^"']+ class truncated it to 11 bytes, which was then served as an icon."""
    import re as _re
    HREF = _re.compile(r'href=(["\'])(.*?)\1', _re.I | _re.S)
    tag = ('<link rel="icon" href="data:image/svg+xml,'
           "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
           '<text>H</text></svg>">')
    got = HREF.search(tag).group(2)
    assert got.endswith("</svg>"), got[-40:]
    assert len(got) > 60



def _xbox_live(title="Forza Horizon 4", in_game="on", tag="yukon9cornelius"):
    """Xbox Live presence entities, exactly as the `xbox` integration exposes them."""
    return {
        f"sensor.{tag}_now_playing": {
            "state": title, "last_updated": iso(time.time()),
            "attributes": {"friendly_name": "Yukon9Cornelius Now playing",
                           "platform": "Xbox One", "progress": "12 %"}},
        f"binary_sensor.{tag}_in_game": {
            "state": in_game, "last_updated": iso(time.time()),
            "attributes": {"friendly_name": "Yukon9Cornelius In game"}},
    }


def test_now_playing_shows_the_xbox_game_from_live_presence():
    """The console's own entities vanish when remote features are off; presence survives."""
    from app.panels import now_playing_panel
    p = now_playing_panel(_xbox_live())
    assert any("Forza Horizon 4" in str(r.value) for r in p.readings), \
        f"expected the game on the card, got {[str(r.value) for r in p.readings]}"
    assert any(row["where"] == "Xbox" for row in p.rows)


def test_now_playing_ignores_xbox_presence_when_not_in_game():
    from app.panels import now_playing_panel
    assert not now_playing_panel(_xbox_live(title="unknown", in_game="off")).rows


def test_now_playing_ignores_an_unknown_title_even_while_in_game():
    """in_game can go true a beat before the title arrives; do not print 'unknown'."""
    from app.panels import now_playing_panel
    assert not now_playing_panel(_xbox_live(title="unknown", in_game="on")).rows


def test_now_playing_never_reports_the_storage_sensor_as_a_game():
    """Regression: a sensor.*xbox* scan once matched storage and showed '802' as the game."""
    from app.panels import now_playing_panel
    states = dict(_xbox_live())
    states["sensor.office_xbox_total_space_internal_storage"] = {
        "state": "802", "last_updated": iso(time.time()), "attributes": {}}
    p = now_playing_panel(states)
    assert not any("802" in str(r.value) for r in p.readings), \
        f"storage leaked onto the card: {[str(r.value) for r in p.readings]}"


def test_now_playing_carries_absolute_artwork_urls():
    """entity_picture is a relative signed path; the browser needs a reachable host."""
    from app.panels import now_playing_panel, HA_PUBLIC
    states = {"media_player.kitchen": {
        "state": "playing", "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "Kitchen", "media_title": "Unwritten",
                       "media_artist": "Natasha Bedingfield",
                       "entity_picture": "/api/media_player_proxy/media_player.kitchen?token=abc"}}}
    art = now_playing_panel(states).rows[0]["art"]
    assert art.startswith(("http://", "https://")), f"art must be absolute, got {art!r}"
    assert art.endswith("?token=abc") and art.startswith(HA_PUBLIC)


def test_now_playing_leaves_an_already_absolute_art_url_alone():
    """PSN hands back a full CDN URL; prefixing it would break the image."""
    from app.panels import now_playing_panel
    cdn = "https://image.api.playstation.com/vulcan/ap/rnd/x.png"
    states = {"media_player.office_playstation_5_pro": {
        "state": "playing", "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "PlayStation 5 Pro",
                       "media_title": "Horizon Forbidden West", "entity_picture": cdn}}}
    assert now_playing_panel(states).rows[0]["art"] == cdn


def test_now_playing_row_without_artwork_is_not_broken():
    from app.panels import now_playing_panel
    states = {"media_player.x": {"state": "playing", "last_updated": iso(time.time()),
                                 "attributes": {"friendly_name": "X", "media_title": "T"}}}
    assert now_playing_panel(states).rows[0]["art"] is None


def test_now_playing_falls_back_to_the_tvos_app_icon():
    """An Apple TV on a live channel reports an app but no entity_picture."""
    from app.panels import now_playing_panel
    states = {"media_player.primary_bedroom": {
        "state": "playing", "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "Primary Bedroom",
                       "media_title": "Western Mass News on ABC40",
                       "app_id": "com.google.ios.youtubeunplugged", "app_name": "YouTube TV"}}}
    icons = {"com.google.ios.youtubeunplugged": "https://is1-ssl.mzstatic.com/x/512.png"}
    assert now_playing_panel(states, None, icons).rows[0]["art"] == \
        "https://is1-ssl.mzstatic.com/x/512.png"


def test_real_artwork_beats_the_app_icon():
    """When the player has proper art, the app icon must not override it."""
    from app.panels import now_playing_panel
    states = {"media_player.family_room": {
        "state": "playing", "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "Family Room", "media_title": "A Film",
                       "app_id": "com.netflix.Netflix",
                       "entity_picture": "/api/media_player_proxy/media_player.family_room?token=t"}}}
    icons = {"com.netflix.Netflix": "https://is1-ssl.mzstatic.com/netflix.png"}
    art = now_playing_panel(states, None, icons).rows[0]["art"]
    assert art.endswith("?token=t"), f"real artwork should win, got {art!r}"


def test_now_playing_separates_device_type_from_location():
    """Friendly names conflate the two: sams_bedroom is a Sonos whose AREA is Office."""
    from app.panels import now_playing_panel
    states = {"media_player.sams_bedroom": {
        "state": "playing", "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "Sam's Bedroom", "media_title": "Unwritten"}}}
    row = now_playing_panel(states,
                            {"media_player.sams_bedroom": "Sonos"},
                            None,
                            {"media_player.sams_bedroom": "Office"}).rows[0]
    assert row["dev"] == "Sonos", row
    assert row["loc"] == "Office", row


def test_now_playing_tolerates_a_player_with_no_area():
    """A roaming speaker belongs to no room; that must not be guessed or crash."""
    from app.panels import now_playing_panel
    states = {"media_player.sonos_roam": {
        "state": "playing", "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "Sonos Roam", "media_title": "T"}}}
    row = now_playing_panel(states, {"media_player.sonos_roam": "Sonos"}).rows[0]
    assert row["loc"] == "" and row["dev"] == "Sonos"


def test_excluded_players_never_reach_the_card(monkeypatch=None):
    """A sleep-sound machine is a media_player by type and noise by intent."""
    from app import panels as P
    states = {"media_player.robins_hatch_media_player": {
        "state": "playing", "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "Robin's Hatch", "media_title": "White Noise"}}}
    before = P.NOWPLAYING_EXCLUDE
    try:
        P.NOWPLAYING_EXCLUDE = {"media_player.robins_hatch_media_player"}
        assert not P.now_playing_panel(states).rows
    finally:
        P.NOWPLAYING_EXCLUDE = before
    # and with no exclusion configured it behaves normally
    assert P.now_playing_panel(states).rows


def test_device_types_carry_their_brand_logo():
    from app.panels import now_playing_panel, BRAND_LOGOS
    for kind, want in (("Sonos", "sonos"), ("Apple TV", "apple"),
                       ("PlayStation", "playstation")):
        states = {"media_player.x": {"state": "playing", "last_updated": iso(time.time()),
                                     "attributes": {"friendly_name": "X", "media_title": "T"}}}
        row = now_playing_panel(states, {"media_player.x": kind}).rows[0]
        assert row["logo"] == want, f"{kind} -> {row['logo']!r}"
    assert BRAND_LOGOS["Xbox"] == "xbox"


def test_an_unmapped_device_type_has_no_logo_but_keeps_its_name():
    """Cast/DLNA have no mark; a missing logo must never blank the device line."""
    from app.panels import now_playing_panel
    states = {"media_player.x": {"state": "playing", "last_updated": iso(time.time()),
                                 "attributes": {"friendly_name": "X", "media_title": "T"}}}
    row = now_playing_panel(states, {"media_player.x": "Cast"}).rows[0]
    assert row["logo"] is None and row["dev"] == "Cast"


def test_every_brand_logo_id_exists_in_the_sprite():
    """A typo'd symbol id renders as an invisible gap, not an error -- so assert it."""
    from pathlib import Path
    from app.panels import BRAND_LOGOS
    root = Path(__file__).resolve().parents[1]
    sprite = (root / "app/templates/_brands.svg").read_text()
    page = (root / "app/templates/index.html").read_text()
    # external <use href="file.svg#id"> is unsupported in Chrome/Safari,
    # so the sprite must be inlined and referenced same-document
    assert "{% include '_brands.svg' %}" in page
    assert "brands.svg#" not in page
    for name in BRAND_LOGOS.values():
        assert f'id="brand-{name}"' in sprite, f"missing symbol brand-{name}"


def test_xbox_row_takes_its_location_from_the_area_registry():
    """The console sensor has no area in HA today; setting one must just work."""
    from app.panels import now_playing_panel
    states = {"sensor.box_now_playing": {"state": "Forza", "last_updated": iso(time.time()),
                                         "attributes": {}},
              "binary_sensor.box_in_game": {"state": "on", "last_updated": iso(time.time()),
                                            "attributes": {}}}
    row = [r for r in now_playing_panel(states, {}, None,
                                        {"sensor.box_now_playing": "Office"}).rows
           if r["dev"] == "Xbox"][0]
    assert row["loc"] == "Office" and row["logo"] == "xbox"
    bare = [r for r in now_playing_panel(states, {}).rows if r["dev"] == "Xbox"][0]
    assert bare["loc"] == ""


# --- gaming: three currencies, and a zero that means "not seen" ----------------
def _feed(**over):
    d = {"generated_epoch": time.time(),
         "ra": {"retro_points": 126145, "games": 1904, "points": 33580, "rank": 3322,
                "mastered": 76, "beaten": 140},
         "xbox": {"gamerscore": 58215, "games": 193},
         "psn": {"achievements": 267, "games": 30},
         "by_brand": {"Nintendo": {"mastered": 65, "beaten": 128},
                      "Xbox": {"mastered": 24, "beaten": 68},
                      "PlayStation": {"mastered": 2, "beaten": 10}},
         "totals": {"achievements": 9615, "achievements_total": 97987, "hours": 6554,
                    "games": 2547}}
    d.update(over)
    return (time.time(), d)


def test_the_score_strip_is_three_separate_currencies():
    from app.panels import gaming_panel
    scores = [r for r in gaming_panel(_feed()).readings if r.group == "score"]
    assert [r.label for r in scores] == ["RetroPoints", "Gamerscore", "Trophies"]
    assert [r.value for r in scores] == ["126,145", "58,215", "267"]
    # Never summed into a combined total: they are not the same unit.
    assert not any("total" in (r.label or "").lower() for r in scores)


def test_a_zero_gamerscore_is_reported_as_unseen_not_as_zero():
    """GameLog publishes gamerscore 0 when the OpenXBL profile call returns no settings
    block. It does not raise, so nothing is logged, and 0 rendered as a real score."""
    from app.panels import gaming_panel
    gs = [r for r in gaming_panel(_feed(xbox={"gamerscore": 0, "games": 193})).readings
          if r.label == "Gamerscore"][0]
    assert gs.missing and gs.effective_status is Status.UNKNOWN
    assert gs.display() == "—" and "never successfully read" in gs.note


def test_mastered_counts_every_platform_not_just_retroachievements():
    from app.panels import gaming_panel
    got = {r.label: r for r in gaming_panel(_feed()).readings if r.group == "progress"}
    # 65 + 24 + 2, not ra.mastered's 76; and 128 + 68 + 10, not ra.beaten's 140
    assert got["Mastered"].value == 91 and got["Beaten"].value == 206
    assert "every platform" in got["Mastered"].note


def test_most_played_is_gone():
    from app.panels import gaming_panel
    p = gaming_panel(_feed(most_played={"title": "Tiger Woods PGA Tour 2004",
                                        "system": "PS2", "hours": 546}))
    assert not any(r.label == "Most played" for r in p.readings)


# --- storage: the percentage is the value, and it carries the colour -----------
def _snap(pools, hosts=None):
    rows = [{"name": n, "used": u, "avail": a} for n, u, a in pools]
    return (time.time(), {"datasets": rows, "dirs": [], "hosts": hosts or []})


def test_pool_value_is_the_percentage_and_free_space_moves_to_the_note():
    from app.panels import storage_panel
    G = 2 ** 30
    r = [x for x in storage_panel(_snap([("tank", 600 * G, 400 * G)])).readings
         if x.group == "pool"][0]
    assert r.value == "60%"
    assert "400 GB free of" in r.note
    assert "60%" not in r.note, "percentage must not also be repeated in the note"


def test_capacity_colour_tracks_the_percentage():
    from app.panels import storage_panel
    G = 2 ** 30
    got = {r.label: r.status for r in
           storage_panel(_snap([("a", 10 * G, 90 * G), ("b", 85 * G, 15 * G),
                                ("c", 95 * G, 5 * G)])).readings if r.group == "pool"}
    assert got == {"a": Status.OK, "b": Status.WARN, "c": Status.ALERT}


def test_headline_does_not_repeat_what_every_reading_already_says():
    from app.panels import storage_panel
    G = 2 ** 30
    p = storage_panel(_snap([("tank", 600 * G, 400 * G), ("HPP", 10 * G, 90 * G)]))
    assert "most used is tank at 60%" in p.headline
    assert "free" not in p.headline


def test_node_local_storage_is_listed_and_shared_datastores_are_not():
    """pbs-backup appears under all three nodes -- the same bytes, seen three times."""
    from app.panels import storage_panel
    G = 2 ** 30
    pve = [{"node": "pve3", "storage": "local", "shared": 0, "disk": 18 * G, "maxdisk": 94 * G},
           {"node": "pve3", "storage": "local-lvm", "shared": 0, "disk": 16 * G, "maxdisk": 349 * G},
           {"node": "pve3", "storage": "pbs-backup", "shared": 1, "disk": 133 * G, "maxdisk": 228 * G},
           {"node": "pve", "storage": "tank", "shared": 0, "disk": 1 * G, "maxdisk": 2 * G}]
    hosts = [r for r in storage_panel(_snap([("z", 1 * G, 9 * G)]), pve).readings
             if r.group == "host"]
    assert [r.label for r in hosts] == ["pve3 · root", "pve3 · VM disks"]
    assert hosts[0].value == "19%"


def test_a_self_reporting_host_appears_and_goes_unknown_when_it_stops():
    """gaming-pc is a desktop, not a cluster member -- it pushes, and it sleeps."""
    from app.panels import storage_panel
    G = 2 ** 30
    fresh = _snap([("z", 1 * G, 9 * G)],
                  hosts=[{"host": "gaming-pc", "label": "root", "used": 24 * G,
                          "total": 235 * G, "as_of": time.time()}])
    r = [x for x in storage_panel(fresh).readings if x.group == "host"][0]
    assert r.label == "gaming-pc · root" and r.value == "10%"
    stale = _snap([("z", 1 * G, 9 * G)],
                  hosts=[{"host": "gaming-pc", "label": "root", "used": 24 * G,
                          "total": 235 * G, "as_of": time.time() - 30 * 3600}])
    r = [x for x in storage_panel(stale).readings if x.group == "host"][0]
    assert r.effective_status is Status.UNKNOWN, "an asleep desktop must not read as fine"


# --- security: per-network counts, not a subset of HA trackers ----------------
def _client(net, name=None, essid="", wired=False, age_days=400, ip="192.168.1.9"):
    return {"mac": "aa:bb", "ip": ip, "named": bool(name),
            "label": name or "unknown-thing", "oui": "Acme", "network": net,
            "essid": essid, "wired": wired, "guest": net == "Guest",
            "first_seen": time.time() - age_days * 86400, "last_seen": time.time()}


def _sec(clients):
    from app.config import Config
    return security_panel({**_house(), "cover.garage_door": {"state": "closed"}},
                          clients, Config())


def test_a_new_unnamed_device_on_a_trusted_network_warns():
    p = _sec([_client("Home", None, age_days=2, ip="192.168.1.77")])
    r = [x for x in p.readings if x.label == "Unrecognised arrivals"][0]
    assert r.value == 1 and r.status is Status.WARN and "192.168.1.77" in r.note


def test_long_standing_unnamed_devices_do_not_warn_forever():
    """13 devices here are permanently unnamed -- Proxmox containers, the Xbox, a
    laptop. Warning on all of them would sit amber forever and be ignored in a week."""
    p = _sec([_client("Home", None, age_days=400) for _ in range(13)])
    r = [x for x in p.readings if x.label == "Unrecognised arrivals"][0]
    assert r.value == 0 and r.status is Status.OK


def test_a_new_device_that_someone_named_is_not_a_stranger():
    p = _sec([_client("Home", "Robin iPad", age_days=1)])
    assert [x for x in p.readings if x.label == "Unrecognised arrivals"][0].value == 0


def test_a_new_unnamed_device_on_the_iot_network_is_not_a_stranger():
    """IoT is not in the trusted list: things land there constantly by design."""
    p = _sec([_client("IoT", None, essid="IoT-Net", age_days=1)])
    assert [x for x in p.readings if x.label == "Unrecognised arrivals"][0].value == 0


def _house():
    """The sensors every Security test needs present.

    leak_readings always emits a sump reading, and reports UNKNOWN when the sensor is
    absent — a missing sump sensor is exactly the blind spot this project refuses to
    render as fine. So fixtures supply one rather than the panel suppressing it."""
    return {"sensor.sump_pump_voltage": {"state": "117", "last_updated": iso(time.time())}}


def _door(state, dc="door"):
    return {"state": state, "last_changed": iso(time.time()),
            "last_updated": iso(time.time()),
            "attributes": {"device_class": dc, "friendly_name": "A Door"}}


def _contacts(**states):
    """Contact sensors as Home Assistant reports them: everything device_class door,
    and every friendly name carrying the same useless "Entry Door" suffix."""
    out = {}
    for k, v in states.items():
        d = _door(v)
        pretty = k.replace("_entry_door", "").replace("_", " ").capitalize()
        d["attributes"]["friendly_name"] = f"{pretty} Entry Door"
        out[f"binary_sensor.{k}"] = d
    return out


def _with_doors(fn):
    """Run with the four real doors configured, as production does."""
    import app.panels as P
    real = P.SECURITY_DOORS
    P.SECURITY_DOORS = {
        "binary_sensor.front_door_entry_door": "Front Door (Foyer)",
        "binary_sensor.fr_garage_entry_door": "Garage Door (Family Room)",
        "binary_sensor.fr_slider_entry_door": "Family Room Slider (Family Room)",
        "binary_sensor.basement_entry_door": "Hatchway (Basement)",
    }
    try:
        return fn()
    finally:
        P.SECURITY_DOORS = real


def test_windows_are_split_from_doors_despite_ha_calling_them_all_doors():
    """The contacts were installed from one device profile, so HA reports ten "doors"
    for a house with four. Nothing in the data distinguishes them."""
    st = {**_house(), **_contacts(
        front_door_entry_door="off", fr_garage_entry_door="off",
        fr_slider_entry_door="off", basement_entry_door="off",
        office_back_entry_door="off", dining_r_entry_door="off",
        dining_l_entry_door="off")}
    p = _with_doors(lambda: security_panel(st))
    labels = {r.label: r for r in p.readings}
    assert "Entry doors ×4" in labels and "Windows ×3" in labels
    # When everything is shut, the useful thing is which openings are covered.
    assert "Front Door (Foyer)" in labels["Entry doors ×4"].note
    assert "Dining R" in labels["Windows ×3"].note, labels["Windows ×3"].note


def test_an_open_window_is_named_and_reaches_the_headline():
    st = {**_house(), **_contacts(front_door_entry_door="off",
                                  dining_l_entry_door="on")}
    p = _with_doors(lambda: security_panel(st))
    w = [r for r in p.readings if r.label.startswith("Windows")][0]
    assert w.value == "1 open" and w.status is Status.WARN and w.note == "Dining L"
    assert "Dining L open" in p.headline


def test_an_appliance_door_is_not_a_way_into_the_house():
    st = {**_house(), "binary_sensor.whirlpool_dryer_door": _door("on"),
          **_contacts(front_door_entry_door="off")}
    r = [x for x in _with_doors(lambda: security_panel(st)).readings
         if x.label.startswith("Entry doors")][0]
    assert r.label == "Entry doors ×1" and r.value == "All closed"


def test_the_entry_door_suffix_is_stripped_from_every_label():
    """"Dining r Entry Door" — the suffix is on all of them and is wrong on the windows."""
    from app.panels import _contact_label
    st = {"binary_sensor.dining_r_entry_door": _door("off")}
    st["binary_sensor.dining_r_entry_door"]["attributes"]["friendly_name"] = "Dining r Entry Door"
    assert _contact_label("binary_sensor.dining_r_entry_door", st) == "Dining R"


def test_dead_august_locks_are_stated_once_and_do_not_score():
    """Known dead with a replacement ordered -- an alarm nobody can clear is noise."""
    st = {**_house(),
          "lock.garage_door": {"state": "unavailable", "attributes": {}},
          "lock.august_smart_lock_pro_3rd_gen": {"state": "unavailable", "attributes": {}}}
    p = security_panel(st)
    r = [x for x in p.readings if x.label.startswith("Door locks")][0]
    assert r.value == "offline" and r.informational
    assert p.rollup() is Status.OK, "a documented-dead lock must not pin the card amber"


def test_a_disarmed_alarm_is_not_a_fault_but_a_triggered_one_is():
    base = {**_house(), "binary_sensor.front_door_entry_door": _door("off")}
    calm = security_panel({**base, "alarm_control_panel.x": {
        "state": "disarmed", "last_changed": iso(time.time()), "attributes": {}}})
    assert [r for r in calm.readings if r.label == "Alarm system"][0].status is Status.OK
    fired = security_panel({**base, "alarm_control_panel.x": {
        "state": "triggered", "last_changed": iso(time.time()), "attributes": {}}})
    assert fired.rollup() is Status.ALERT


def test_water_outranks_doors_in_the_headline():
    """Folding leak into Security must not bury the worst thing behind 'doors closed'."""
    now = time.time()
    p = security_panel({
        "binary_sensor.laundry_leak_moisture": {"state": "on", "last_updated": iso(now)},
        "sensor.sump_pump_voltage": {"state": "117", "last_updated": iso(now)},
        "cover.garage_door": {"state": "open", "last_changed": iso(now), "attributes": {}},
    })
    assert p.headline.startswith("WATER DETECTED")
    assert p.rollup() is Status.ALERT


def test_security_no_longer_counts_network_clients():
    from app.config import Config
    st = {**_house(), "binary_sensor.front_door_entry_door": _door("off")}
    p = security_panel(st, [], Config())
    assert not any(r.group == "net" for r in p.readings)


# --- network health -----------------------------------------------------------
def _radio(util, band="2.4 GHz", interference=2, satisfaction=97):
    return {"band": band, "channel": 6, "util": util, "interference": interference,
            "satisfaction": satisfaction, "clients": 9}


def test_client_counts_moved_here_and_the_guest_network_still_warns():
    from app.config import Config
    from app.panels import network_panel
    clients = [_client("Home", "NAS", wired=True),
               _client("Guest", "Visitor Phone", essid="ExampleGuest")]
    p = network_panel(clients, {}, [], Config())
    got = {r.label: r for r in p.readings if r.group == "net"}
    assert got["Home"].value == 1
    assert got["Guest"].status is Status.WARN and "Visitor Phone" in got["Guest"].note


def test_a_radio_busy_with_our_own_traffic_is_not_a_fault():
    """The false alarm this rule was rewritten for: Family Room 5 GHz hit 88% airtime
    and the card went red while its clients reported 99% satisfaction and nearly all of
    that airtime was our own download."""
    from app.config import Config
    from app.panels import network_panel
    p = network_panel([], {}, [{"name": "Family Room AP", "type": "uap", "state": 1,
                                "radios": [_radio(88, interference=1, satisfaction=99)]}],
                      Config())
    assert not any(r.label == "Radio interference" for r in p.readings)
    assert p.rows[0]["state"] == "ok"
    assert p.rollup() is Status.OK


def test_airtime_we_do_not_control_is_the_thing_that_is_flagged():
    """A neighbour on our channel cannot be fixed by doing less."""
    from app.config import Config
    from app.panels import network_panel
    p = network_panel([], {}, [{"name": "Upstairs AP", "type": "uap", "state": 1,
                                "radios": [_radio(55, interference=45, satisfaction=95)]}],
                      Config())
    r = [x for x in p.readings if x.label == "Radio interference"][0]
    # WARN, not ALERT, since 2026-09-07. Severe outside airtime is still flagged -- that
    # is this test's point and it still holds -- but with satisfaction at 95 nobody is
    # suffering, and an emergency colour for a neighbour's wifi is not actionable. The
    # escalation to ALERT now belongs to the clients: see
    # test_unhappy_clients_are_flagged_even_on_a_quiet_channel.
    assert r.status is Status.WARN and "45% airtime from outside" in r.note
    assert "Upstairs AP" in p.headline


def test_unhappy_clients_are_flagged_even_on_a_quiet_channel():
    from app.config import Config
    from app.panels import network_panel
    p = network_panel([], {}, [{"name": "Bedroom AP", "type": "uap", "state": 1,
                                "radios": [_radio(12, interference=1, satisfaction=61)]}],
                      Config())
    r = [x for x in p.readings if x.label == "Radio interference"][0]
    assert r.status is Status.ALERT and "61% satisfaction" in r.note


def test_the_headline_cannot_say_healthy_while_the_chip_says_alert():
    from app.config import Config
    from app.panels import network_panel
    p = network_panel([], {}, [{"name": "Bedroom AP", "type": "uap", "state": 1,
                                "radios": [_radio(12, interference=1, satisfaction=61)]}],
                      Config())
    assert "healthy" not in p.headline and p.rollup() is not Status.OK

def test_an_offline_access_point_is_an_alert():
    from app.config import Config
    from app.panels import network_panel
    p = network_panel([], {}, [{"name": "Family Room AP", "type": "uap", "state": 0,
                                "radios": []}], Config())
    r = [x for x in p.readings if x.label == "UniFi devices"][0]
    assert r.status is Status.ALERT and "Family Room AP offline" in r.note
    assert p.headline == "UniFi device offline"


def test_a_stale_speedtest_is_not_presented_as_current():
    from app.config import Config
    from app.panels import network_panel
    old = time.time() - 5 * 86400
    p = network_panel([], {"www": {"status": "ok", "latency": 19, "xput_down": 832.0,
                                   "xput_up": 40.0, "speedtest_lastrun": old}}, [], Config())
    r = [x for x in p.readings if x.label == "Throughput"][0]
    assert r.effective_status is Status.UNKNOWN, "a 5-day-old figure is not today's speed"


def test_network_panel_says_so_when_the_controller_is_unreachable():
    from app.config import Config
    from app.panels import network_panel
    assert network_panel(None, {}, [], Config()).error


# --- now playing: the mark is the device name ---------------------------------
def test_the_xbox_row_carries_its_configured_console_and_room():
    """HA's Xbox device is the *account* ("Xbox Network"), never the box in the room."""
    import app.panels as P
    from app.panels import now_playing_panel
    real = P.NOWPLAYING_DEVICES
    P.NOWPLAYING_DEVICES = {"box": ("Xbox Series X", "Office")}
    try:
        states = {"sensor.box_now_playing": {"state": "Forza", "attributes": {},
                                             "last_updated": iso(time.time())},
                  "binary_sensor.box_in_game": {"state": "on", "attributes": {},
                                                "last_updated": iso(time.time())}}
        row = [r for r in now_playing_panel(states, {}).rows if r["dev"] == "Xbox"][0]
        assert row["loc"] == "Office" and row["detail"] == "Xbox Series X"
    finally:
        P.NOWPLAYING_DEVICES = real


def test_a_grouped_speaker_names_its_companions_on_the_location_line():
    """The '+3' that rode on the device line said this less precisely, and next to a
    brand mark it read as part of the logo."""
    from app.panels import now_playing_panel
    states = {
        "media_player.office": {"state": "playing", "last_updated": iso(time.time()),
                                "attributes": {"friendly_name": "Office", "media_title": "T",
                                               "group_members": ["media_player.office",
                                                                 "media_player.kitchen"]}},
        "media_player.kitchen": {"state": "playing", "last_updated": iso(time.time()),
                                 "attributes": {"friendly_name": "Kitchen", "media_title": "T",
                                                "group_members": ["media_player.office",
                                                                  "media_player.kitchen"]}},
    }
    kinds = {"media_player.office": "Sonos", "media_player.kitchen": "Sonos"}
    rows = now_playing_panel(states, kinds).rows
    assert len(rows) == 1, "a group is one row"
    assert rows[0]["logo"] == "sonos"
    assert rows[0]["detail"] in ("with Kitchen", "with Office")


# --- homelab: what survived the Consoles card ---------------------------------
def test_playstation_firmware_moved_to_homelab_and_does_not_score():
    from app.panels import homelab_panel
    ps = [{"ip": "1.2.3.4", "host_type": "PS5", "name": "PS5-174", "version": "13.60"}]
    p = homelab_panel({"nodes": [], "guests": [], "backups": {}}, {}, 90,
                      None, None, ps)
    r = [x for x in p.readings if "firmware" in x.label.lower()][0]
    assert r.value == "13.60" and r.informational


def test_ha_updates_row_counts_only_what_ha_can_install():
    """Plex's update entity is notify-only (supported_features=16): the server lives on
    CT108, HA cannot install it and HA's Updates page does not list it. It belongs with
    the self-hosted apps, not in the HA / firmware count."""
    from app.panels import homelab_panel
    states = {
        "update.joeflix_update": {"state": "on", "attributes": {"supported_features": 16}},
        "update.home_assistant_core_update": {"state": "on",
                                              "attributes": {"supported_features": 15}},
        "update.ha_mcp_tools_update": {"state": "off",   # skipped in HA, reads off
                                       "attributes": {"supported_features": 23}},
    }
    p = homelab_panel({"nodes": [], "guests": [], "backups": {}}, states, 90,
                      patch=None, apps=None)
    r = [x for x in p.readings if x.label == "HA / firmware updates"][0]
    assert r.value == 1
    assert "joeflix" not in r.note.lower()


def _health(checks, age=0.0):
    import time as _t
    return (_t.time() - age, {"services": {"nextcloud": {"checks": checks,
                                                         "as_of": _t.time() - age}}})


def test_a_failing_service_check_warns_and_names_it():
    """The 2026-08-30 case: editing broken for 13 days while the card said Nextcloud was
    current and its container was up. A version is not health."""
    from app.panels import homelab_panel
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(),
                      health=_health([{"key": "editing", "label": "Document editing",
                                       "state": "fail", "detail": "mixed content blocked"},
                                      {"key": "web", "label": "Web reachable", "state": "ok"}]))
    r = next(r for r in p.readings if r.label == "Service checks")
    assert r.effective_status is Status.WARN, r
    assert r.value == "1 failing"
    assert "Document editing" in r.note and "mixed content" in r.note, r.note


def test_service_advice_does_not_turn_the_card_yellow():
    """`occ setupchecks` always has opinions -- a missing security header is advice, not a
    fault. Same rule as updates that apply themselves: warn only on what needs someone."""
    from app.panels import homelab_panel
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(),
                      health=_health([{"key": "web", "label": "Web reachable", "state": "ok"},
                                      {"key": "headers", "label": "Setup checks",
                                       "state": "warn", "detail": "HSTS not set"},
                                      {"key": "sc", "label": "Setup checks", "state": "info",
                                       "detail": "65 checks"}]))
    r = next(r for r in p.readings if r.label == "Service checks")
    assert r.effective_status is Status.OK, r
    assert "1 advisory" in r.note, r.note


def test_a_stale_health_push_is_unknown_not_healthy():
    """A checker that stopped running must not keep reporting its last good answer."""
    from app.panels import homelab_panel
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(),
                      health=_health([{"key": "web", "state": "ok"}], age=6 * 3600))
    r = next(r for r in p.readings if r.label == "Service checks")
    assert r.effective_status is Status.UNKNOWN, r
    assert "stale" in r.note and "nextcloud" in r.note, r.note


def test_no_health_report_adds_no_reading():
    """Not deployed is not the same as unobservable — the same rule the app report follows."""
    from app.panels import homelab_panel
    p = homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch())
    assert not any(r.label == "Service checks" for r in p.readings)


def test_an_ignored_app_is_dropped_from_the_list():
    """Homepage used the default and is now decommissioned outright, so the list is
    empty -- but the mechanism has to keep working for the next retired app."""
    import app.panels as P
    from app.panels import homelab_panel
    real = P.APPS_IGNORE
    P.APPS_IGNORE = {"oldthing"}
    try:
        apps = (time.time(), {"apps": [{"name": "oldthing", "state": "ok", "current": "1"},
                                       {"name": "immich", "state": "ok", "current": "3.1.0"}]})
        p = homelab_panel({"nodes": [], "guests": [], "backups": {}}, {}, 90, None, apps)
        assert [r["name"] for r in p.app_rows] == ["immich"]
    finally:
        P.APPS_IGNORE = real


def test_a_cached_gamerscore_ages_from_when_it_was_actually_read():
    """GameLog reuses the last good value when OpenXBL answers without one. That is
    honest; presenting it as freshly read would not be."""
    from app.panels import gaming_panel
    fresh = _feed(xbox={"gamerscore": 58215, "games": 193,
                        "gamerscore_as_of": time.time() - 3600})
    r = [x for x in gaming_panel(fresh).readings if x.label == "Gamerscore"][0]
    assert r.value == "58,215" and r.effective_status is Status.OK
    old = _feed(xbox={"gamerscore": 58215, "games": 193,
                      "gamerscore_as_of": time.time() - 9 * 86400})
    r = [x for x in gaming_panel(old).readings if x.label == "Gamerscore"][0]
    assert r.effective_status is Status.UNKNOWN, "a 9-day-old read is not today's score"


def test_a_hosts_only_storage_push_is_accepted():
    """A desktop reports its own filesystems and has no ZFS datasets to send."""
    from app.panels import storage_panel
    G = 2 ** 30
    snap = (time.time(), {"datasets": [{"name": "z", "used": G, "avail": 9 * G}],
                          "dirs": [],
                          "hosts": [{"host": "gaming-pc", "label": "root",
                                     "used": 25 * G, "total": 235 * G,
                                     "as_of": time.time()}]})
    r = [x for x in storage_panel(snap).readings if x.label == "gaming-pc · root"][0]
    assert r.value == "11%"


def test_a_host_reporting_several_filesystems_keeps_all_of_them():
    """Keyed on host name alone, gaming-pc's root and /boot collapsed to one entry and the
    last in the payload won -- a 235 GB disk shown as a 4 GB EFI partition."""
    from app.panels import storage_panel
    G = 2 ** 30
    now = time.time()
    snap = (now, {"datasets": [{"name": "z", "used": G, "avail": 9 * G}], "dirs": [],
                  "hosts": [{"host": "gaming-pc", "label": "root", "used": 25 * G,
                             "total": 235 * G, "as_of": now},
                            {"host": "gaming-pc", "label": "/boot", "used": G,
                             "total": 4 * G, "as_of": now}]})
    got = {r.label: r.value for r in storage_panel(snap).readings if r.group == "host"}
    assert got == {"gaming-pc · root": "11%", "gaming-pc · /boot": "25%"}


# --- security: quiet things fold away, problems do not -----------------------
def test_only_the_alarm_and_smoke_stay_on_the_face_of_a_calm_card():
    st = {**_house(), **_contacts(front_door_entry_door="off", dining_l_entry_door="off"),
          "alarm_control_panel.x": {"state": "disarmed", "attributes": {},
                                    "last_changed": iso(time.time())}}
    p = _with_doors(lambda: security_panel(st))
    shown = [r.label for r in p.readings if r.group != "quiet"]
    # "Doors & windows" earns its permanent place next to the alarm: "disarmed" and
    # "everything shut" are two different questions, and the second one is the one
    # you actually want answered on the way to bed.
    assert shown == ["Alarm system", "Doors & windows"], shown
    assert any(r.label.startswith("Entry doors") and r.group == "quiet" for r in p.readings)


def test_anything_that_needs_attention_is_promoted_back_out():
    st = {**_house(), **_contacts(front_door_entry_door="on", dining_l_entry_door="off")}
    p = _with_doors(lambda: security_panel(st))
    shown = {r.label for r in p.readings if r.group != "quiet"}
    assert any(l.startswith("Entry doors") for l in shown), shown
    assert not any(l.startswith("Windows") for l in shown), "a shut window is not news"


def test_a_sensor_that_has_gone_quiet_is_not_folded_away():
    """effective_status, not status: a stale reading is exactly what must stay visible."""
    now = time.time()
    st = {"sensor.sump_pump_voltage": {"state": "117", "last_updated": iso(now - 900_000)},
          **_contacts(front_door_entry_door="off")}
    p = _with_doors(lambda: security_panel(st))
    sump = [r for r in p.readings if r.label == "Sump pump supply"][0]
    assert sump.effective_status is Status.UNKNOWN and sump.group != "quiet"


def test_presence_is_gone():
    st = {**_house(), "person.alex": {"state": "home", "last_changed": iso(time.time()),
                                     "attributes": {"friendly_name": "Alex Example"}}}
    assert not any("Alex" in r.label for r in security_panel(st).readings)


# --- storage: the row is the bar ---------------------------------------------
def test_capacity_readings_carry_a_bar_percentage():
    from app.panels import storage_panel
    G = 2 ** 30
    r = [x for x in storage_panel(_snap([("HPP", 17 * G, 83 * G)])).readings
         if x.group == "pool"][0]
    assert r.bar == 17.0 and r.value == "17%"


def test_a_reading_with_no_capacity_has_no_bar():
    """Only a value that is a proportion of something gets drawn as one."""
    from app.panels import gaming_panel
    assert all(r.bar is None for r in gaming_panel(_feed()).readings)


# --- gaming: three currencies, three marks -----------------------------------
def test_each_score_carries_the_mark_for_its_own_currency():
    from app.panels import gaming_panel
    marks = {r.label: r.mark for r in gaming_panel(_feed()).readings if r.group == "score"}
    assert marks == {"RetroPoints": "ra", "Gamerscore": "g", "Trophies": "trophy"}


def test_the_trophy_symbol_exists_in_the_sprite():
    from pathlib import Path
    sprite = (Path(__file__).resolve().parents[1] / "app/templates/_brands.svg").read_text()
    assert 'id="brand-trophy"' in sprite


# --- homelab absorbed the Home Assistant card --------------------------------
def test_dead_devices_moved_to_homelab_and_partial_ones_are_not_counted():
    """UniFi Network shows 72 of 104 entities unavailable and is demonstrably reachable."""
    from app.panels import homelab_panel
    pve = {"nodes": [], "guests": [], "backups": {}}
    p = homelab_panel(pve, {}, 90, None, None, None,
                      {"Ecobee": (12, 12), "UniFi Network": (72, 104)})
    r = [x for x in p.readings if x.label == "Devices not responding"][0]
    assert r.value == 1 and r.status is Status.ALERT
    assert [d["device"] for d in p.dead_rows] == ["Ecobee"]


def test_homelab_says_all_answering_rather_than_going_quiet():
    from app.panels import homelab_panel
    p = homelab_panel({"nodes": [], "guests": [], "backups": {}}, {}, 90,
                      None, None, None, {"UniFi Network": (72, 104)})
    r = [x for x in p.readings if x.label == "Devices not responding"][0]
    assert r.value == 0 and r.status is Status.OK and "idle entities" in r.note


def test_zero_unrecognised_arrivals_folds_away_like_everything_else_that_is_fine():
    from app.config import Config
    st = {**_house(), **_contacts(front_door_entry_door="off"),
          "alarm_control_panel.x": {"state": "disarmed", "attributes": {},
                                    "last_changed": iso(time.time())}}
    calm = _with_doors(lambda: security_panel(st, [], Config()))
    assert ([r.label for r in calm.readings if r.group != "quiet"]
            == ["Alarm system", "Doors & windows"])
    busy = _with_doors(lambda: security_panel(
        st, [_client("Home", None, age_days=1)], Config()))
    assert "Unrecognised arrivals" in [r.label for r in busy.readings if r.group != "quiet"]


def test_mastered_and_beaten_are_two_boxes_with_their_own_marks():
    """One line reading "Mastered 102 · 206 beaten" made the second a footnote."""
    from app.panels import gaming_panel
    got = {r.label: r.mark for r in gaming_panel(_feed()).readings if r.group == "progress"}
    assert got == {"Mastered": "crown", "Beaten": "check"}


def test_the_family_becomes_one_box_per_person():
    from app.panels import gaming_panel
    fam = {"Pop": {"mastered": 101, "beaten": 125}, "Sam": {"mastered": 2, "beaten": 5},
           "Robin": {"mastered": 1, "beaten": 3}, "Momma": {"mastered": 0, "beaten": 3}}
    rows = [r for r in gaming_panel(_feed(family=fam)).readings if r.group == "family"]
    assert [r.label for r in rows] == ["Pop", "Sam", "Robin", "Momma"]
    assert rows[0].value == 101 and "125 beaten" in rows[0].note
    # A person with nothing mastered still gets a box -- an absent box reads as absent
    # from the house, not as a zero.
    assert rows[3].value == 0


def test_every_mark_used_by_a_panel_exists_in_the_sprite():
    """A typo'd symbol id renders as an invisible gap rather than an error."""
    from pathlib import Path
    from app.panels import gaming_panel
    sprite = (Path(__file__).resolve().parents[1] / "app/templates/_brands.svg").read_text()
    svg_marks = {"trophy", "crown", "check"}
    for r in gaming_panel(_feed()).readings:
        if r.mark in svg_marks:
            assert f'id="brand-{r.mark}"' in sprite, r.mark


def test_a_local_scratch_pool_is_not_a_share():
    """Scratch is a pool to ZFS, but nobody stores anything in it on purpose."""
    from app.panels import storage_panel
    G = 2 ** 30
    p = storage_panel(_snap([("tank", 600 * G, 400 * G), ("Scratch", 0, 180 * G)]))
    face = [r.label for r in p.readings if r.group == "pool"]
    folded = [r.label for r in p.readings if r.group == "host"]
    assert face == ["tank"]
    assert folded == ["pve · Scratch"]
    # and it stays out of the headline, which is about where the bulk data lives
    assert "1 pool healthy — most used is tank at 60%" == p.headline


def test_a_local_pool_filling_up_still_warns():
    from app.panels import storage_panel
    G = 2 ** 30
    p = storage_panel(_snap([("tank", G, 9 * G), ("Scratch", 95 * G, 5 * G)]))
    r = [x for x in p.readings if x.label == "pve · Scratch"][0]
    assert r.status is Status.ALERT
    assert "Scratch over 80% full" in p.headline


def test_a_pool_is_labelled_with_whoever_reported_the_datasets():
    """A desktop pushing only its own filesystems must not relabel the storage host's
    pools: gaming-pc's hosts-only push turned pve's Scratch into "gaming-pc · Scratch"."""
    from app.panels import storage_panel
    G = 2 ** 30
    snap = (time.time(), {"datasets": [{"name": "Scratch", "used": 0, "avail": 180 * G},
                                       {"name": "tank", "used": G, "avail": 9 * G}],
                          "dirs": [], "source": "pve",
                          "hosts": [{"host": "gaming-pc", "label": "root", "used": G,
                                     "total": 10 * G, "as_of": time.time()}]})
    labels = {r.label for r in storage_panel(snap).readings}
    assert "pve · Scratch" in labels and "gaming-pc · root" in labels


# --- the banner names what is wrong ------------------------------------------
def test_a_healthy_page_lists_nothing():
    from app.panels import attention_items
    from app.model import Panel
    assert attention_items([Panel(key="a", title="A")]) == []


def test_the_reason_comes_from_the_readings_not_the_headline():
    """HVAC's headline is what the house is doing ("Bedroom Minisplit cooling") -- true,
    current, and nothing to do with why the card is amber."""
    from app.panels import attention_items
    from app.model import Panel
    p = Panel(key="hvac", title="HVAC", headline="Bedroom Minisplit cooling",
              readings=[Reading("Living Room", 72, as_of=time.time()),
                        Reading("Range hood grease filter", "7%", as_of=time.time(),
                                status=Status.WARN)])
    item = attention_items([p])[0]
    assert item["why"] == "Range hood grease filter"
    assert item["title"] == "HVAC" and item["key"] == "hvac"


def test_worst_first_within_a_card_and_across_them():
    from app.panels import attention_items
    from app.model import Panel
    warn = Panel(key="w", title="Warny",
                 readings=[Reading("mild", 1, as_of=time.time(), status=Status.WARN)])
    bad = Panel(key="b", title="Alarmy",
                readings=[Reading("nagging", 1, as_of=time.time(), status=Status.WARN),
                          Reading("on fire", 1, as_of=time.time(), status=Status.ALERT)])
    got = attention_items([warn, bad])
    assert [i["title"] for i in got] == ["Alarmy", "Warny"]
    assert got[0]["why"].startswith("on fire"), got[0]["why"]


def test_an_unreachable_card_gives_its_error_as_the_reason():
    from app.panels import attention_items
    from app.model import Panel
    p = Panel(key="x", title="X", error="Home Assistant unreachable")
    assert attention_items([p])[0]["why"] == "Home Assistant unreachable"


def test_an_informational_reading_is_never_offered_as_the_reason():
    """It is excluded from the roll-up, so it must not be blamed for one."""
    from app.panels import attention_items
    from app.model import Panel
    p = Panel(key="x", title="X", headline="all fine",
              readings=[Reading("PS5 storage", "not exposed", informational=True),
                        Reading("real problem", None)])
    assert attention_items([p])[0]["why"] == "real problem"


def test_every_listed_card_can_be_linked_to():
    """The list anchors to the card's id, so a key that is not a real panel id would
    produce a link that goes nowhere."""
    import app.main as M
    from app.panels import attention_items
    panels = M.build_panels()
    ids = {p.key for p in panels}
    assert all(a["key"] in ids for a in attention_items(panels))


def test_ignored_devices_accept_patterns_for_ephemeral_clients():
    """Plex mints a media_player per connecting client and marks it unavailable the
    moment it disconnects, so an exact-match list would need an entry per device."""
    import app.panels as P
    from app.panels import homelab_panel
    real = P.HA_IGNORE_DEVICES
    P.HA_IGNORE_DEVICES = {"Plex (*", "Sonos Roam"}
    try:
        pve = {"nodes": [], "guests": [], "backups": {}}
        dead = {"Plex (Plex for Apple TV - Apple TV)": (1, 1),
                "Plex (Chrome - Windows)": (1, 1),
                "Sonos Roam": (3, 3),
                "Ecobee": (12, 12)}
        p = homelab_panel(pve, {}, 90, None, None, None, dead)
        r = [x for x in p.readings if x.label == "Devices not responding"][0]
        assert r.value == 1, [d["device"] for d in p.dead_rows]
        assert [d["device"] for d in p.dead_rows] == ["Ecobee"]
    finally:
        P.HA_IGNORE_DEVICES = real


def test_an_exact_name_without_wildcards_still_matches_exactly():
    """A plain entry must not become a substring match."""
    from app.panels import _ignored_device
    import app.panels as P
    real = P.HA_IGNORE_DEVICES
    P.HA_IGNORE_DEVICES = {"Sonos Roam"}
    try:
        assert _ignored_device("Sonos Roam")
        assert not _ignored_device("Sonos Roam 2")
        assert not _ignored_device("Roam")
    finally:
        P.HA_IGNORE_DEVICES = real


# --- favourites -------------------------------------------------------------
def test_favourites_have_a_name_url_and_reachable_icon_slug():
    from app.panels import favourites
    f = favourites()
    assert len(f) == 10
    assert [x["name"] for x in f][:3] == ["Proxmox", "UniFi", "NextDNS"]
    for x in f:
        assert x["url"].startswith("http")
        assert x["icon"].startswith("https://cdn.jsdelivr.net/") and x["icon"].endswith(".png")


def test_favourites_can_be_overridden_from_the_environment():
    import os, importlib
    import app.panels as P
    os.environ["FAVORITES"] = "Foo|https://foo.test|foo, Bar|https://bar.test|bar"
    try:
        f = P.favourites()
        assert [x["name"] for x in f] == ["Foo", "Bar"]
        assert f[0]["icon"].endswith("/foo.png")
    finally:
        del os.environ["FAVORITES"]


def test_a_malformed_override_falls_back_rather_than_rendering_broken_tiles():
    import os
    import app.panels as P
    os.environ["FAVORITES"] = "just-a-name,another|missing-icon"
    try:
        assert len(P.favourites()) == 10     # the built-in list, not two broken entries
    finally:
        del os.environ["FAVORITES"]


def test_a_tagged_bookmark_becomes_a_favourite_tile():
    from app.panels import favourites
    rows = [{"title": "Proxmox", "url": "https://proxmox.example.com", "tags": "favorite,infra"},
            {"title": "Some Blog", "url": "https://example.test/x", "tags": "reading"}]
    f = favourites(rows)
    assert [x["name"] for x in f] == ["Proxmox"]
    assert f[0]["icon"].endswith("/proxmox.png")


def test_the_tag_test_is_exact_not_a_substring():
    """"favorites-old" must not light up the strip."""
    from app.panels import favourites_from_bookmarks
    rows = [{"title": "X", "url": "https://x.test", "tags": "favorites-old"},
            {"title": "Y", "url": "https://y.test", "tags": "unfavorite"}]
    assert favourites_from_bookmarks(rows) == []


def test_an_unmapped_host_falls_back_to_the_cached_favicon():
    from app.panels import favourites
    f = favourites([{"title": "Odd", "url": "https://odd.example.test/x", "tags": "favorite"}])
    assert f[0]["icon"] == "/favicon/odd.example.test"


def test_no_tagged_bookmarks_keeps_the_builtin_list():
    """An empty tag must not silently wipe the strip off the page."""
    from app.panels import favourites
    assert len(favourites([{"title": "X", "url": "https://x.test", "tags": "reading"}])) == 10
    assert len(favourites([])) == 10


def test_the_bookmark_table_gains_tags_on_an_existing_database():
    """CREATE TABLE IF NOT EXISTS is a no-op on a live db; the column needs a migration."""
    import sqlite3, tempfile, os as _os
    from app.db import Database
    d = tempfile.mkdtemp(); path = _os.path.join(d, "old.db")
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE bookmark (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual',
        added_ts INTEGER NOT NULL);""")
    c.execute("INSERT INTO bookmark (url,title,added_ts) VALUES ('https://a.test','A',0)")
    c.commit(); c.close()
    db = Database(path)                                  # must migrate, not crash
    db.upsert_bookmark("https://b.test", "B", tags="favorite")
    got = {b["url"]: b.get("tags") for b in db.bookmarks()}
    assert got["https://b.test"] == "favorite"
    assert got["https://a.test"] == ""                   # existing row keeps a default




def test_playstation_playing_appears_exactly_once():
    """Regression: the PS5 was appended twice — once for being awake and once for
    having a title — so the duplicate ONLY showed up mid-game. Settled for good by
    removing the second source; the HA media_player entity is now the only one."""
    from app.panels import now_playing_panel
    st = {"media_player.ps5_174": {
        "state": "playing", "last_changed": iso(time.time()),
        "attributes": {"friendly_name": "PS5-174", "media_title": "Astro Bot"}}}
    hits = [r for r in now_playing_panel(st).rows if "PS5-174" in str(r.get("where"))]
    assert len(hits) == 1, hits
    assert hits[0]["what"] == "Astro Bot"


def test_playstation_never_claims_the_title_is_unreported():
    """The old filler said 'on, no title reported', which read as a Sony limitation.
    It is not one — never ship that phrasing again. Aimed at the HA entity now, since
    that is where a PS5 with no media_title comes from."""
    from app.panels import now_playing_panel
    for attrs in ({"friendly_name": "PS5-174"},
                  {"friendly_name": "PS5-174", "media_title": "Astro Bot"}):
        for state in ("on", "playing", "idle"):
            p = now_playing_panel({"media_player.ps5_174": {
                "state": state, "last_changed": iso(time.time()), "attributes": attrs}})
            assert not any("no title reported" in str(r.get("what") or "")
                           for r in p.rows)


# --- game collection (GameVault) --------------------------------------------

def _gamevault(valuation_finished: float, reconcile_finished: float | None = None) -> tuple:
    """A minimal GameVault payload: one holding, one price, two job runs."""
    return (
        time.time(),
        {"num_holdings": 1, "num_wanted": 4,
         "latest": {"num_owned": 1, "total_actual_c": 507906,
                    "total_loose_c": 415835, "total_new_c": 885276},
         "runs": {
             "valuation": {"ok": 1, "finished_at": iso(valuation_finished),
                           "detail": "priced 203, errors 0"},
             "reconcile": {"ok": 1, "detail": "added 25 games",
                           "finished_at": iso(reconcile_finished
                                              if reconcile_finished is not None
                                              else valuation_finished)},
         }},
        {"games": [{"title": "Sneak King", "platform_name": "Xbox 360",
                    "condition": "cib", "quantity": 1, "value_c": 0,
                    "acquired_at": iso(time.time() - 86400)}]},
    )


def test_a_stale_pricing_run_does_not_assert_a_collection_value():
    """The dollar figure is only as current as the job that produced it. GameVault
    prices daily; a fortnight of silence means we no longer know what the shelf is
    worth, and saying "$5,079" anyway is the manufactured confidence this dashboard
    exists to refuse."""
    fresh = collection_panel(_gamevault(time.time() - 3600))
    value = next(r for r in fresh.readings if r.label == "Value")
    assert value.effective_status is Status.OK and value.display() == "$5,079"

    stale = collection_panel(_gamevault(time.time() - 14 * 86400))
    value = next(r for r in stale.readings if r.label == "Value")
    assert value.effective_status is Status.UNKNOWN
    assert stale.rollup() is Status.UNKNOWN, "a card cannot be OK on a number it can't see"


def test_the_shelf_count_is_not_aged_like_the_price():
    """The shelf scan runs Mondays and the pricing run daily, so on a Thursday the
    count is legitimately three days old. Ageing both against one threshold would
    either cry wolf about the count or wave through a week-old valuation."""
    p = collection_panel(_gamevault(time.time() - 3600,
                                    reconcile_finished=time.time() - 3 * 86400))
    owned = next(r for r in p.readings if r.label == "Owned")
    assert owned.effective_status is Status.OK
    assert p.rollup() is Status.OK


def test_an_unpriced_game_is_not_worth_zero():
    """Five holdings have no market price yet. Rendering those as $0 would state that
    a game is worthless, which is a claim GameVault never made."""
    p = collection_panel(_gamevault(time.time() - 3600))
    assert p.app_rows[0]["value"] is None
    assert p.rows[0]["value"] is None


def test_gamevault_being_unreachable_is_unknown_not_an_empty_shelf():
    p = collection_panel(None)
    assert p.rollup() is Status.UNKNOWN and p.error
    assert "0" not in p.headline, "an outage must never read as a collection of nothing"


def test_the_totals_survive_losing_the_per_item_call():
    """Two endpoints, and the breakdown is the expendable one: totals without a
    platform table is a smaller card, no card at all is a worse answer."""
    fetched, dash, _ = _gamevault(time.time() - 3600)
    p = collection_panel((fetched, dash, {}))
    assert p.rollup() is Status.OK
    assert next(r for r in p.readings if r.label == "Owned").display() == "1"
    assert p.rows == []


def test_patch_debt_tolerance_matches_the_push_cadence():
    """PATCH_MAX_AGE was 26h against a 3-hourly push -- eight missed pushes still rendered as
    a confident number, and on 2026-09-03 the card showed 16 pending for three hours after
    they had been applied. The tolerance must stay within a few multiples of the cadence, or
    the card silently reports history as current."""
    import inspect
    from app import panels
    src = inspect.getsource(panels.homelab_panel)
    m = re.search(r"PATCH_MAX_AGE = (\d+) \* 3600", src)
    assert m, "PATCH_MAX_AGE no longer declared as hours"
    hours = int(m.group(1))
    assert hours <= 9, f"PATCH_MAX_AGE is {hours}h; the push runs 3-hourly, so this hides staleness"


def test_a_new_billing_cycle_is_unknown_not_a_zero_forecast():
    """Opower zeroes usage AND forecast the moment the cycle rolls over, and does not
    post the new cycle for a few days. Read literally that produced the headline
    "On pace for 0 kWh — 100% under a typical month" on 2026-09-06."""
    from app.config import Config
    from app.panels import power_panel
    p = power_panel(_power_states({"networkrack": 294}, forecast_kwh=0), Config())
    assert "0 kWh" not in (p.headline or ""), p.headline
    assert "under a typical month" not in (p.headline or ""), p.headline
    f = [r for r in p.readings if r.label == "Forecast this cycle"][0]
    assert f.status is Status.UNKNOWN and f.value is None
    assert f.informational, "a routine monthly utility gap must not pin the card amber"
    assert p.rollup() is not Status.UNKNOWN, "the metered circuits are live and known"


def test_a_real_forecast_still_reports_normally():
    """The zero-guard must not swallow a genuine forecast."""
    from app.config import Config
    from app.panels import power_panel
    p = power_panel(_power_states({"networkrack": 294}, forecast_kwh=1622), Config())
    assert "On pace for" in (p.headline or "")
    assert not [r for r in p.readings
                if r.label == "Forecast this cycle" and r.status is Status.UNKNOWN]


def test_a_locks_own_doorsense_is_not_a_door_contact():
    """The Yale reports binary_sensor.garage_door_lock `unknown` unless DoorSense is
    calibrated. It duplicates fr_garage_entry_door, and on 2026-09-06 it turned the
    whole Security card UNKNOWN a week after the lock went in."""
    st = {**_house(),
          "lock.garage_door_lock": {"state": "locked", "attributes": {}},
          "binary_sensor.garage_door_lock": _door("unknown"),
          **_contacts(front_door_entry_door="off", fr_garage_entry_door="off")}
    p = _with_doors(lambda: security_panel(st))
    dw = [r for r in p.readings if r.label == "Doors & windows"][0]
    assert dw.status is Status.OK, dw.note
    assert "Garage Door Lock" not in dw.note, dw.note
    assert p.rollup() is not Status.UNKNOWN


def test_the_locks_row_describes_the_live_lock_not_the_retired_august_stack():
    st = {**_house(),
          "lock.garage_door_lock": {"state": "locked",
                                    "attributes": {"friendly_name": "Garage Door Lock"}},
          **_contacts(front_door_entry_door="off")}
    r = [x for x in _with_doors(lambda: security_panel(st)).readings
         if x.label.startswith("Door locks")][0]
    assert r.value == "1 of 1 locked", r.value
    assert "locked" in r.note and "August" not in r.note, r.note
    assert "not reporting: 0" not in r.note, "healthy must not read as a fault"
    assert r.status is Status.OK and not r.informational



def _appletv_plus_plex_session():
    """The real 2026-09-06 pair, verbatim from HA: an Apple TV playing from Plex, seen
    by the Apple TV integration AND by Plex's own session entity. Note the titles
    genuinely differ -- Plex appends the year -- and only Plex's row lacks an area."""
    return {
        "media_player.living_room": _mp(
            "playing", name="Living Room Apple TV", app_name="Plex",
            app_id="com.plexapp.plex", media_title="Minions & Monsters",
            media_content_type="video", media_duration=5559, media_position=3847),
        "media_player.plex_plex_for_apple_tv_apple_tv_4": _mp(
            "playing", name="Plex (Plex for Apple TV - Apple TV)",
            media_title="Minions & Monsters (2026)", media_content_type="movie",
            media_duration=5559, media_position=3846),
    }


def test_one_screen_playing_one_film_is_one_row():
    """Two integrations watching the same playback put the film on the card twice --
    the second row rendering with the bare fallback device label "Media"."""
    from app.panels import now_playing_panel
    p = now_playing_panel(_appletv_plus_plex_session(),
                          {"media_player.living_room": "Apple TV"},
                          None,
                          {"media_player.living_room": "Living Room"})
    assert len(p.rows) == 1, [r["where"] for r in p.rows]
    assert not [r for r in p.rows if r["dev"] == "Media"], p.rows


def test_the_surviving_row_is_the_one_that_knows_the_room():
    """A room is the useful fact on a house dashboard; the Plex session entity has none."""
    from app.panels import now_playing_panel
    row = now_playing_panel(_appletv_plus_plex_session(),
                            {"media_player.living_room": "Apple TV"},
                            None,
                            {"media_player.living_room": "Living Room"}).rows[0]
    assert row["loc"] == "Living Room", row
    assert row["dev"] == "Apple TV", row


def test_duplicate_streams_are_matched_on_duration_not_title():
    """Plex appends the year, so title matching would miss this entirely."""
    st = _appletv_plus_plex_session()
    a = st["media_player.living_room"]["attributes"]["media_title"]
    b = st["media_player.plex_plex_for_apple_tv_apple_tv_4"]["attributes"]["media_title"]
    assert a != b, "the fixture must keep the real mismatched titles"


def test_two_different_programmes_of_the_same_length_stay_separate():
    """Duration alone is not identity -- position must agree too, or a coincidence of
    runtime would silently swallow a second thing that is genuinely playing."""
    from app.panels import now_playing_panel
    states = {
        "media_player.living_room": _mp(
            "playing", name="Living Room Apple TV", media_title="Film A",
            media_duration=5559, media_position=120),
        "media_player.office_office": _mp(
            "playing", name="Office Apple TV", media_title="Film B",
            media_duration=5559, media_position=4800),
    }
    p = now_playing_panel(states)
    assert len(p.rows) == 2, [r["what"] for r in p.rows]


def test_players_without_duration_are_never_collapsed():
    """Live TV and speakers report no duration; they must not all fold into one row."""
    from app.panels import now_playing_panel
    states = {
        "media_player.family_room_2": _mp("playing", name="Family Room Apple TV",
                                          media_title="Western Mass News"),
        "media_player.kitchen": _mp("playing", name="Kitchen", media_title="Light Switch"),
    }
    assert len(now_playing_panel(states).rows) == 2


def test_media_duration_is_read_as_a_number_not_a_state_dict():
    """`num()` takes a STATE DICT and reads ["state"]; handed a bare int it returns None,
    which would make the de-duplication silently never fire."""
    from app.panels import _secs
    assert _secs(5559) == 5559.0
    assert _secs("5559") == 5559.0
    assert _secs(None) is None and _secs("n/a") is None



def test_esde_game_appears_in_now_playing():
    from app.panels import now_playing_panel
    snap = (time.time(), {"state": "playing", "game": "Sonic the Hedgehog 2",
                          "system": "megadrive", "system_full": "Sega Mega Drive",
                          "art": "aGk="})
    row = now_playing_panel({"media_player.x": _mp("off")}, esde=snap).rows[0]
    assert row["what"] == "Sonic the Hedgehog 2"
    assert row["dev"] == "ES-DE" and row["loc"] == "Sega Mega Drive"
    assert row["art"].startswith("/api/esde/art?v="), row["art"]


def test_esde_row_falls_back_to_the_short_system_name():
    from app.panels import now_playing_panel
    snap = (time.time(), {"state": "playing", "game": "Ridge Racer", "system": "psx"})
    assert now_playing_panel({}, esde=snap).rows[0]["loc"] == "psx"


def test_esde_without_art_still_shows_the_game():
    from app.panels import now_playing_panel
    snap = (time.time(), {"state": "playing", "game": "Doom"})
    row = now_playing_panel({}, esde=snap).rows[0]
    assert row["what"] == "Doom" and row["art"] is None


def test_a_stopped_esde_push_shows_nothing():
    from app.panels import now_playing_panel
    assert now_playing_panel({}, esde=(time.time(), {"state": "stopped"})).rows == []


def test_a_stale_esde_push_is_not_still_playing():
    """`game-end` never fires on a crash or a power cut. A card claiming you are mid-game
    days later is worse than one that says nothing."""
    from app.panels import now_playing_panel, ESDE_MAX_AGE
    old = (time.time() - ESDE_MAX_AGE - 60,
           {"state": "playing", "game": "Sonic the Hedgehog 2"})
    assert now_playing_panel({}, esde=old).rows == []
    fresh = (time.time() - 60, {"state": "playing", "game": "Sonic the Hedgehog 2"})
    assert len(now_playing_panel({}, esde=fresh).rows) == 1


def test_esde_never_displaces_a_real_media_player():
    """The console rows are additive; a film and a game can be on at once."""
    from app.panels import now_playing_panel
    st = {"media_player.living_room": _mp("playing", name="Living Room Apple TV",
                                          media_title="Minions & Monsters",
                                          media_duration=5559, media_position=10)}
    p = now_playing_panel(st, esde=(time.time(), {"state": "playing", "game": "Doom"}))
    assert {r["what"] for r in p.rows} == {"Minions & Monsters", "Doom"}


def test_esde_with_no_game_name_is_ignored():
    from app.panels import now_playing_panel
    assert now_playing_panel({}, esde=(time.time(), {"state": "playing", "game": " "})).rows == []



def test_ha_being_down_does_not_hide_a_game_esde_pushed_directly():
    """The ES-DE feed does not go through Home Assistant, so an HA outage must not
    erase a game we genuinely know is running. Both facts are reported: the game, and
    that the media players are unknown."""
    from app.panels import now_playing_panel
    p = now_playing_panel({}, esde=(time.time(), {"state": "playing", "game": "Doom"}))
    assert p.error, "the HA outage must still be reported"
    assert [r["what"] for r in p.rows] == ["Doom"]



def _ng_states(circuits, forecast, poll_age_s=60, forecast_state=None):
    """Power states plus the opower poll clock, which is what tells an empty cycle
    apart from a dead feed."""
    st = _power_states(circuits, forecast_kwh=forecast)
    if forecast_state is not None:
        k = "sensor.national_grid_current_bill_electric_forecasted_usage"
        if forecast_state == "__absent__":
            st.pop(k, None)
        else:
            st[k]["state"] = forecast_state
    st["sensor.national_grid_last_updated"] = {
        "state": iso(time.time() - poll_age_s), "last_updated": iso(time.time()),
        "attributes": {}}
    return st


def test_an_empty_forecast_on_a_healthy_feed_stays_quiet():
    """Start of every billing cycle. An actionless alert once a month is noise."""
    from app.config import Config
    from app.panels import power_panel
    p = power_panel(_ng_states({"networkrack": 294}, 0), Config())
    r = [x for x in p.readings if x.label == "Forecast this cycle"][0]
    assert r.informational, "a routine monthly gap must not colour the card"
    assert p.rollup() is not Status.UNKNOWN
    assert "new billing cycle" in r.note


def test_a_dead_feed_is_reported_even_though_it_also_reads_empty():
    """Same zero on the screen, completely different meaning -- and this one is
    actionable, so it must NOT be informational."""
    from app.config import Config
    from app.panels import power_panel
    p = power_panel(_ng_states({"networkrack": 294}, 0, forecast_state="unavailable"),
                    Config())
    r = [x for x in p.readings if x.label == "Forecast this cycle"][0]
    assert not r.informational, "a broken pull must be visible"
    assert r.status is Status.UNKNOWN
    assert "not reporting" in r.note and "not a new billing cycle" in r.note


def test_a_missing_forecast_sensor_counts_as_a_dead_feed():
    from app.config import Config
    from app.panels import power_panel
    p = power_panel(_ng_states({"networkrack": 294}, 0, forecast_state="__absent__"),
                    Config())
    r = [x for x in p.readings if x.label == "Forecast this cycle"][0]
    assert not r.informational and r.status is Status.UNKNOWN


def test_a_feed_that_stopped_polling_is_reported_even_holding_a_number():
    """The numbers can look plausible while the integration has quietly stopped."""
    from app.config import Config
    from app.panels import power_panel, NG_FEED_MAX_AGE
    st = _ng_states({"networkrack": 294}, 0, poll_age_s=NG_FEED_MAX_AGE + 3600)
    r = [x for x in power_panel(st, Config()).readings
         if x.label == "Forecast this cycle"][0]
    assert not r.informational, "a stalled pull must be visible"
    assert "no successful poll" in r.note, r.note


def test_no_poll_clock_does_not_invent_a_stalled_feed():
    """Absent evidence is not evidence of failure."""
    from app.config import Config
    from app.panels import power_panel
    st = _ng_states({"networkrack": 294}, 0)
    del st["sensor.national_grid_last_updated"]
    r = [x for x in power_panel(st, Config()).readings
         if x.label == "Forecast this cycle"][0]
    assert r.informational, "with no clock, fall back to presence -- not a verdict"



def _with_registry(mapping, fn):
    """Run fn with HA's device map installed, then restore. The registry is module
    state, so leaking it would silently change every later test."""
    from app.liveness import set_device_registry
    try:
        set_device_registry(mapping)
        return fn()
    finally:
        set_device_registry({})


def test_device_key_prefers_the_real_device_over_the_name_guess():
    """The measured 2026-09-07 failure: `_active_power` strips to `..._active`, an
    orphan key its 10 device-mates never share, so the circuit could never be
    refreshed by a sibling and a constant 0 W read as 'stopped reporting' forever."""
    from app.liveness import device_key
    naive = device_key("sensor.primary_en_suite_active_power")
    assert naive == "primary_en_suite_active", naive
    assert naive != device_key("sensor.primary_en_suite_energy")
    reg = {"sensor.primary_en_suite_active_power": "dev1",
           "sensor.primary_en_suite_energy": "dev1"}
    same = _with_registry(reg, lambda: (
        device_key("sensor.primary_en_suite_active_power"),
        device_key("sensor.primary_en_suite_energy")))
    assert same == ("dev1", "dev1"), same


def test_an_alive_device_is_not_stale_just_because_one_channel_is_constant():
    """The whole point. The speaker idles at 0 W for a day while its energy counter
    keeps ticking; the device is alive and the circuit must say so."""
    from app.liveness import build_last_seen, device_key
    now = time.time()
    states = {
        "sensor.primary_en_suite_active_power": {
            "state": "0.0", "last_updated": iso(now - 25 * 3600), "attributes": {}},
        "sensor.primary_en_suite_energy": {
            "state": "3.7", "last_updated": iso(now - 60), "attributes": {}},
    }
    reg = {e: "dev1" for e in states}

    def age():
        seen = build_last_seen(states)
        return now - seen[device_key("sensor.primary_en_suite_active_power")]

    assert age() > 24 * 3600, "without the registry the guess still looks stale"
    assert _with_registry(reg, age) < 300, "with it, the live sibling carries the device"


def test_entities_with_no_device_still_fall_back_to_the_name_heuristic():
    """Template sensors and helpers have no device; they must not become keyless."""
    from app.liveness import device_key
    reg = {"sensor.sump_pump_voltage": "dev9"}
    got = _with_registry(reg, lambda: device_key("sensor.sump_pump_power"))
    assert got == "sump_pump", got


def test_an_empty_registry_changes_nothing():
    """HA unreachable must degrade to the old behaviour, not break liveness."""
    from app.liveness import device_key
    before = device_key("sensor.sump_pump_voltage")
    assert _with_registry({}, lambda: device_key("sensor.sump_pump_voltage")) == before


def test_the_sump_supply_reading_is_not_keyed_on_a_literal_name():
    """The highest-consequence reading in the house. A hardcoded 'sump_pump' key
    silently misses once liveness groups by device id."""
    import inspect
    from app import panels
    src = inspect.getsource(panels.leak_readings) if hasattr(panels, "leak_readings") \
        else inspect.getsource(panels)
    assert 'last_seen.get("sump_pump")' not in src, \
        "hardcoded device key would miss once keys become device ids"



def _minisplit(watts):
    """A Samsung minisplit: reports NO hvac_action, so its power draw is the only
    evidence of whether it is running."""
    return {
        "climate.primary_bedroom_bedroom_minisplit": {
            "state": "cool", "last_updated": iso(time.time()),
            "attributes": {"friendly_name": "Bedroom Minisplit",
                           "current_temperature": 66}},
        "sensor.primary_bedroom_bedroom_minisplit_power": {
            "state": str(watts), "last_updated": iso(time.time()),
            "attributes": {"device_class": "power", "unit_of_measurement": "W"}},
    }


def test_a_minisplit_power_lookup_survives_the_device_registry():
    """REGRESSION 2026-09-07. device_key returns an opaque HA device id once the
    registry loads, so building `sensor.<key>_power` from it matched nothing, both
    minisplits became 'no heat/cool state', and HVAC went UNKNOWN for a house that
    was simply idle. The lookup must use name_stem."""
    from app.panels import hvac_panel
    from app.liveness import set_device_registry
    # 900 W on purpose: only a value above the running threshold makes a SUCCESSFUL
    # lookup observable. With device_key the id would be `sensor.abc123def456_power`,
    # which matches nothing, and the zone would fall back to unknown.
    st = _minisplit(900)
    reg = {e: "abc123def456" for e in st}
    try:
        set_device_registry(reg)
        p = hvac_panel(st)
    finally:
        set_device_registry({})
    assert p.rows[0]["action"] == "cooling", \
        f"power lookup must survive the registry, got {p.rows[0]}"
    assert p.status is not Status.UNKNOWN, p.headline


def test_name_stem_is_not_device_key_once_a_registry_is_loaded():
    from app.liveness import name_stem, device_key, set_device_registry
    e = "climate.primary_bedroom_bedroom_minisplit"
    try:
        set_device_registry({e: "abc123def456"})
        assert device_key(e) == "abc123def456"
        assert name_stem(e) == "primary_bedroom_bedroom_minisplit"
    finally:
        set_device_registry({})


def test_a_running_minisplit_is_still_detected_from_its_power_draw():
    """High power is the one thing the sensor CAN prove: hundreds of watts cannot
    happen at standby, so it positively establishes the unit is running."""
    from app.panels import hvac_panel
    p = hvac_panel(_minisplit(900))
    assert "Idle" not in p.headline, p.headline
    assert p.rows[0]["action"] == "cooling", p.rows[0]


def test_an_unreporting_zone_headline_leads_with_the_blind_spot():
    """The colour and the words must agree. "Nothing known to be running" read as
    reassurance while the card sat UNKNOWN."""
    from app.panels import hvac_panel
    st = {"climate.mystery": {"state": "cool", "last_updated": iso(time.time()),
                              "attributes": {"friendly_name": "Mystery Zone",
                                             "current_temperature": 70}}}
    p = hvac_panel(st)
    assert p.status is Status.UNKNOWN
    assert "Nothing known to be running" not in p.headline
    assert "Can't tell" in p.headline and "Mystery" in p.headline, p.headline



def _samsung(mode, current, target):
    """A Samsung minisplit as SmartThings actually presents it, verified against the
    app 2026-09-07: mode, setpoint and room temperature, and NO hvac_action at all."""
    return {"climate.bedroom_minisplit": {
        "state": mode, "last_updated": iso(time.time()),
        "attributes": {"friendly_name": "Bedroom Minisplit",
                       "current_temperature": current, "temperature": target}}}


def test_a_zone_past_its_setpoint_reads_satisfied_not_idle():
    """"Idle" said nothing about why. Satisfied says the unit is on and has nothing to do."""
    from app.panels import hvac_panel
    p = hvac_panel(_samsung("cool", 66, 70))
    row = [r for r in p.rows if r["name"] == "Bedroom Minisplit"][0]
    assert row["action"] == "satisfied", row
    assert row["mode"] == "cool" and row["target"] == "70"


def test_cool_mode_below_setpoint_is_flagged_as_unreachable():
    """The real 2026-09-07 confusion: both minisplits on, set to 70, room at 66. Cool
    mode cannot warm a room, so that setpoint is unreachable for ever -- and SmartThings
    and HA both happily show "Cool, 70" next to it without a word."""
    from app.panels import hvac_panel
    p = hvac_panel(_samsung("cool", 66, 70))
    r = [x for x in p.readings if x.label == "Setpoint unreachable"]
    assert r, [x.label for x in p.readings]
    # Informational on purpose: nothing is FAILING, the setting is simply the reason
    # nothing happens. Colouring the card for a deliberate configuration would leave a
    # cool-mode standby sitting amber for days with nothing to do about it.
    assert r[0].informational, "a configuration choice is not a fault"
    assert p.rollup() is not Status.WARN, "must not reach the attention banner"
    assert "cool mode cannot warm" in r[0].note, r[0].note
    assert "already past the setpoint" in p.headline, p.headline


def test_heat_mode_above_setpoint_is_flagged_too():
    from app.panels import hvac_panel
    p = hvac_panel(_samsung("heat", 74, 68))
    r = [x for x in p.readings if x.label == "Setpoint unreachable"][0]
    assert "heat mode cannot cool" in r.note, r.note


def test_a_zone_short_of_its_setpoint_is_not_called_idle():
    """Cool mode with the room ABOVE the setpoint: it should be working."""
    from app.panels import hvac_panel
    p = hvac_panel(_samsung("cool", 78, 70))
    row = [r for r in p.rows if r["name"] == "Bedroom Minisplit"][0]
    assert row["action"] == "should be cooling", row
    assert "should be cooling" in p.headline, p.headline
    assert not [x for x in p.readings if x.label == "Setpoint unreachable"]


def test_a_satisfied_zone_in_a_sane_mode_stays_quiet():
    """Heat mode, room already warm enough. Nothing to do and nothing to say."""
    from app.panels import hvac_panel
    p = hvac_panel(_samsung("heat", 70, 70))
    assert p.headline == "Idle — nothing calling for heat or cool", p.headline
    assert not [x for x in p.readings if x.label == "Setpoint unreachable"]


def test_the_action_no_longer_depends_on_the_coarse_power_sensor():
    """That sensor produces ~9 samples in 6h and never exceeds 50 W on these units, so
    it can neither prove nor disprove that a compressor is running."""
    from app.panels import hvac_panel
    st = _samsung("cool", 78, 70)
    st["sensor.bedroom_minisplit_power"] = {
        "state": "4", "last_updated": iso(time.time()),
        "attributes": {"device_class": "power", "unit_of_measurement": "W"}}
    row = [r for r in hvac_panel(st).rows if r["name"] == "Bedroom Minisplit"][0]
    assert row["action"] == "should be cooling", \
        "a 4 W reading must not override mode + setpoint + room temperature"



def _radio_panel(sat, noise, band="ng", ch=1):
    """NB: deliberately NOT named `_radio` -- that name is already a helper above which
    returns a radio DICT, and redefining it silently shadowed the original and broke four
    existing tests. Python takes the last definition; the collision is invisible."""
    from app.config import Config
    from app.panels import network_panel
    # type/state are not decoration: without state == 1 the panel reports the AP as
    # OFFLINE and rolls the card up to ALERT, which made an earlier assertion here pass
    # for entirely the wrong reason.
    devs = [{"name": "Upstairs Hallway AP", "type": "uap", "state": 1, "radios": [
        {"band": band, "channel": ch, "util": 51, "interference": noise,
         "satisfaction": sat, "clients": 13}]}]
    return network_panel({}, {}, devs, Config())


def test_neighbour_interference_alone_does_not_warn_when_clients_are_happy():
    """Measured 2026-09-07: 25-33% outside airtime on channel 1, satisfaction 95, and
    11 of 13 clients between 97 and 100. With 42 other APs visible on that channel and
    three of our own already on 1/6/11, there is nowhere to move to. A permanent amber
    for a neighbour's wifi is an alert nobody can act on."""
    p = _radio_panel(sat=95, noise=33)
    assert p.rows[0]["state"] == "ok", p.rows[0]
    assert p.rollup() is not Status.WARN


def test_unhappy_clients_still_warn_whatever_the_interference():
    """Satisfaction is the honest measure of whether it matters."""
    assert _radio_panel(sat=80, noise=2).rows[0]["state"] == "warn"
    assert _radio_panel(sat=60, noise=2).rows[0]["state"] == "alert"


def test_severe_interference_still_surfaces_before_it_bites():
    """Above NOISE_ALERT it is worth knowing early -- but it is a warn, not an alert:
    if clients are happy it is not an emergency."""
    from app.panels import NOISE_ALERT
    r = _radio_panel(sat=99, noise=NOISE_ALERT + 5).rows[0]
    assert r["state"] == "warn", r


def test_the_interference_number_is_always_shown_even_when_it_does_not_warn():
    """Quieter is not hidden: the figure stays in the table so nothing is lost."""
    r = _radio_panel(sat=95, noise=33).rows[0]
    assert r["interference"] == 33 and r["satisfaction"] == 95



def test_a_one_off_satisfaction_dip_does_not_colour_the_card():
    """Measured 2026-09-08: Primary Bedroom 5 GHz was sampled at 88% airtime and 60%
    satisfaction and the card went red, while UniFi's hourly rollup for that same AP
    never left 95-97 all day. The radios are read once every 15 minutes; one sample is
    not a condition."""
    p = _radio_panel(sat=60, noise=2)          # instantaneous dip
    assert p.rows[0]["state"] == "alert", "without the rollup the sample still decides"
    steady = _radio_panel_with_rollup(sat=60, noise=2, rollup={"Upstairs Hallway AP": 96.4})
    assert steady.rows[0]["state"] == "ok", steady.rows[0]
    assert steady.rollup() is not Status.ALERT


def test_a_sustained_dip_still_alerts():
    """Smoothing must not become deafness: if the HOUR is bad, say so."""
    p = _radio_panel_with_rollup(sat=99, noise=2, rollup={"Upstairs Hallway AP": 61.0})
    assert p.rows[0]["state"] == "alert", p.rows[0]
    r = [x for x in p.readings if x.label == "Radio interference"][0]
    assert "61% satisfaction (hourly)" in r.note, r.note


def test_the_explanation_quotes_the_figure_it_judged_on():
    """Quoting the instantaneous 60% while judging on a sustained 96% would be a lie."""
    p = _radio_panel_with_rollup(sat=60, noise=2, rollup={"Upstairs Hallway AP": 61.0})
    r = [x for x in p.readings if x.label == "Radio interference"][0]
    assert "61%" in r.note and "60%" not in r.note, r.note


def test_an_ap_missing_from_the_rollup_falls_back_to_the_sample():
    """A brand-new AP, or a failed report call, must degrade rather than go blind."""
    p = _radio_panel_with_rollup(sat=60, noise=2, rollup={"Some Other AP": 99.0})
    assert p.rows[0]["state"] == "alert", p.rows[0]


def _radio_panel_with_rollup(sat, noise, rollup, band="ng", ch=1):
    from app.config import Config
    from app.panels import network_panel
    devs = [{"name": "Upstairs Hallway AP", "type": "uap", "state": 1, "radios": [
        {"band": band, "channel": ch, "util": 88, "interference": noise,
         "satisfaction": sat, "clients": 5}]}]
    return network_panel({}, {}, devs, Config(), rollup)



def _patch_report(targets):
    import time as _t
    from app.config import Config
    from app.panels import homelab_panel
    pve={"nodes":[{"node":"pve","status":"online"}],"guests":[],"storage":[],"backups":{}}
    return homelab_panel(pve, {}, 40, (_t.time(), {"targets":targets}))


def _t_host(name,count,sec=0,pkgs=None):
    return {"name":name,"count":count,"security":sec,"reboot":False,"kind":"host",
            "packages":pkgs or []}
def _t_guest(name,count,sec=0,pkgs=None):
    return {"name":name,"count":count,"security":sec,"reboot":False,"kind":"guest",
            "packages":pkgs or []}


def test_host_updates_do_not_warn_the_way_guest_updates_do():
    """The real 2026-09-10 complaint: a warning every morning about three hypervisors
    that nothing is ALLOWED to touch. Hosts are Tier H -- only a planned rolling reboot
    clears them -- so they are scheduled work, not a fault."""
    p=_patch_report([_t_host("pve",5,1,["proxmox-kernel-7.0"]),
                     _t_host("pve3",4,1),_t_host("pve4",4,1)])
    pend=[r for r in p.readings if r.label=="Pending updates"][0]
    assert pend.value==0 and pend.effective_status is Status.OK, pend
    host=[r for r in p.readings if r.label=="Host updates"][0]
    assert host.value==13 and host.informational, host
    assert "KERNEL" in host.note and "Tier H" in host.note, host.note


def test_a_guest_update_that_applies_tonight_does_not_warn():
    """Rewritten 2026-09-11 (was test_a_guest_falling_behind_still_warns, which said any
    guest above zero meant the 05:00 run was broken). Packages that land during the day
    are that run's job; warning about them until then was an alert nobody could act on."""
    p=_patch_report([_t_host("pve",5),_t_guest("ct108 plex",3)])
    pend=[r for r in p.readings if r.label=="Pending updates"][0]
    assert pend.value==3 and pend.effective_status is Status.OK, pend
    assert "3 apply at 05:00" in pend.note, pend.note


def _patch_at(targets, routine, pushed_at):
    from app.panels import homelab_panel
    pve={"nodes":[{"node":"pve","status":"online"}],"guests":[],"storage":[],"backups":{}}
    return homelab_panel(pve, {}, 40, (pushed_at, {"targets":targets, "routine":routine}))


def test_a_guest_the_0500_run_failed_to_update_warns():
    """The signal that matters, made precise: pending before the last 05:00 run started,
    and still pending in a report taken after that run finished."""
    now=time.time()
    g=_t_guest("ct113 radarr",3,pkgs=["libc6"]); g["first_seen"]={"libc6": now-10*3600}
    p=_patch_at([g], {"last_start": now-5*3600, "last_end": now-4.5*3600}, now)
    pend=[r for r in p.readings if r.label=="Pending updates"][0]
    assert pend.effective_status is Status.WARN, pend
    assert "didn't apply at 05:00: ct113 radarr" in pend.note, pend.note
    assert p.rows[0]["tag"]=="missed 05:00"


def test_packages_ubuntu_defers_are_not_a_missed_run():
    """2026-09-17: CT108 and CT114 read "didn't apply at 05:00" every morning while the run
    had done everything it could — the leftovers were Ubuntu PHASED updates and apt
    KEPT-BACK packages (the nvidia set needing the host driver). `installable` is what apt
    would install right now; only those can be overdue."""
    now = time.time()
    g = _t_guest("ct108 plex", 9, pkgs=["libnvidia-compute-580", "krb5-locales"])
    g["first_seen"] = {"libnvidia-compute-580": now - 10 * 3600, "krb5-locales": now - 10 * 3600}
    g["installable"] = []                      # apt would install none of them right now
    p = _patch_at([g], {"last_start": now - 5 * 3600, "last_end": now - 4.5 * 3600}, now)
    r = [x for x in p.readings if x.label == "Pending updates"][0]
    assert r.effective_status is Status.OK, r.note
    assert "didn't apply" not in r.note, r.note
    assert "9 deferred by Ubuntu" in r.note, r.note


def test_an_installable_package_left_behind_still_warns():
    """The signal must survive the fix: something apt CAN install, still pending after a run."""
    now = time.time()
    g = _t_guest("ct113 radarr", 3, pkgs=["libc6", "krb5-locales"])
    g["first_seen"] = {"libc6": now - 10 * 3600, "krb5-locales": now - 10 * 3600}
    g["installable"] = ["libc6"]               # phased krb5 deferred, libc6 is not
    p = _patch_at([g], {"last_start": now - 5 * 3600, "last_end": now - 4.5 * 3600}, now)
    r = [x for x in p.readings if x.label == "Pending updates"][0]
    assert r.effective_status is Status.WARN, r.note
    assert "didn't apply at 05:00: ct113 radarr" in r.note, r.note


def test_an_older_pusher_without_installable_behaves_as_before():
    """No `installable` key = a pre-2026-09-17 payload. Judge every package, as it used to."""
    now = time.time()
    g = _t_guest("ct106 immich", 2, pkgs=["libc6"])
    g["first_seen"] = {"libc6": now - 10 * 3600}
    p = _patch_at([g], {"last_start": now - 5 * 3600, "last_end": now - 4.5 * 3600}, now)
    assert [x for x in p.readings
            if x.label == "Pending updates"][0].effective_status is Status.WARN


def test_a_report_taken_before_the_run_finished_cannot_blame_it():
    """Nothing re-pushes straight after the run, so the last report can predate it. That
    report still lists what the run has since applied -- it is not evidence of a miss."""
    now=time.time()
    g=_t_guest("ct106 immich",2,pkgs=["libc6"]); g["first_seen"]={"libc6": now-10*3600}
    p=_patch_at([g], {"last_start": now-1*3600, "last_end": now-0.5*3600}, now-2*3600)
    pend=[r for r in p.readings if r.label=="Pending updates"][0]
    assert pend.effective_status is Status.OK, pend.note


def test_a_package_that_arrived_after_the_run_started_is_not_blamed_on_it():
    now=time.time()
    g=_t_guest("ct106 immich",1,pkgs=["locales"]); g["first_seen"]={"locales": now-3*3600}
    p=_patch_at([g], {"last_start": now-5*3600, "last_end": now-4.5*3600}, now)
    assert [r for r in p.readings if r.label=="Pending updates"][0].effective_status is Status.OK


def test_a_gated_guest_update_needs_you_straight_away():
    """update-manager holds these for approval, so no 05:00 run will ever clear them."""
    now=time.time()
    g=_t_guest("ct108 plex",1,pkgs=["plexmediaserver"])
    g.update(gated=True, gated_why="critical-service package", first_seen={"plexmediaserver": now})
    p=_patch_at([g], {"last_start": now-5*3600, "last_end": now-4.5*3600}, now)
    pend=[r for r in p.readings if r.label=="Pending updates"][0]
    assert pend.effective_status is Status.WARN, pend
    assert "needs you: ct108 plex (critical-service package)" in pend.note, pend.note


def test_a_0500_run_that_never_happens_is_caught_by_the_backstop():
    """No completed run to compare against (timer off, pve down): a day and a bit is
    long enough that a routine update should have gone."""
    now=time.time()
    g=_t_guest("ct110 prowlarr",1,pkgs=["curl"]); g["first_seen"]={"curl": now-27*3600}
    p=_patch_at([g], None, now)
    assert [r for r in p.readings if r.label=="Pending updates"][0].effective_status is Status.WARN


def test_first_seen_is_carried_between_pushes():
    from app.updates import carry_app_first_seen, carry_patch_first_seen
    new=[{"name":"ct106 immich","packages":["libc6","locales"]}]
    carry_patch_first_seen([{"name":"ct106 immich","first_seen":{"libc6":100}}], new, 500)
    assert new[0]["first_seen"]=={"libc6":100,"locales":500}, new
    apps=[{"name":"immich","state":"update","latest":"3.2.0"},
          {"name":"plex","state":"update","latest":"2"}, {"name":"x","state":"ok"}]
    carry_app_first_seen([{"name":"immich","state":"update","latest":"3.2.0","first_seen":100}],
                         apps, 500)
    assert apps[0]["first_seen"]==100 and apps[1]["first_seen"]==500
    assert "first_seen" not in apps[2]
    newer=[{"name":"immich","state":"update","latest":"3.2.1"}]
    carry_app_first_seen(apps, newer, 900)
    assert newer[0]["first_seen"]==900, "a newer release is a new update"


def _apps_at(entries, routine=None, pushed_at=None):
    from app.panels import homelab_panel
    now=time.time()
    return homelab_panel(_pve_ok(), {}, 60, patch=_fresh_patch(),
                         apps=(pushed_at or now, {"apps":entries, "routine":routine}))


def test_an_app_that_updates_tonight_does_not_warn():
    """The 2026-09-10 evening case: Immich 3.2.0 turned the card yellow at 18:37 for a
    job the 05:00 run was going to do anyway."""
    now=time.time()
    p=_apps_at([{"name":"immich","state":"update","applies":"tonight","first_seen":now-3600},
                {"name":"sonarr","state":"update","applies":"self","first_seen":now-3600},
                {"name":"plex","state":"ok"}])
    r=next(r for r in p.readings if r.label=="Self-hosted apps")
    assert r.effective_status is Status.OK, r.note
    assert "immich applies at 05:00" in r.note and "sonarr updates itself" in r.note, r.note
    assert {a["name"]: a["state"] for a in p.app_rows} == \
        {"immich":"scheduled", "sonarr":"scheduled", "plex":"ok"}
    assert [a["word"] for a in p.app_rows if a["name"]=="sonarr"] == ["self-updating"]


def test_an_app_the_0500_run_missed_warns():
    now=time.time()
    p=_apps_at([{"name":"immich","state":"update","applies":"tonight","first_seen":now-10*3600}],
               routine={"last_start": now-5*3600, "last_end": now-4.5*3600})
    r=next(r for r in p.readings if r.label=="Self-hosted apps")
    assert r.effective_status is Status.WARN and "didn't apply at 05:00: immich" in r.note, r.note
    assert p.app_rows[0]["state"]=="missed"


def test_a_self_updating_app_is_not_held_to_the_0500_clock():
    """Sonarr's own updater runs on its own schedule; only the backstop applies."""
    now=time.time()
    p=_apps_at([{"name":"sonarr","state":"update","applies":"self","first_seen":now-10*3600}],
               routine={"last_start": now-5*3600, "last_end": now-4.5*3600})
    assert next(r for r in p.readings
                if r.label=="Self-hosted apps").effective_status is Status.OK


def test_an_app_that_needs_you_warns_straight_away():
    now=time.time()
    p=_apps_at([{"name":"plex","state":"update","applies":"you","first_seen":now}])
    r=next(r for r in p.readings if r.label=="Self-hosted apps")
    assert r.effective_status is Status.WARN and "needs you: plex" in r.note, r.note


def test_a_held_major_needs_you_even_if_its_line_says_auto():
    now=time.time()
    p=_apps_at([{"name":"immich","state":"update","applies":"tonight","held":True,
                 "first_seen":now}])
    assert next(r for r in p.readings
                if r.label=="Self-hosted apps").effective_status is Status.WARN


def test_the_guest_count_never_includes_hosts():
    p=_patch_report([_t_host("pve",99),_t_guest("ct106 immich",2)])
    pend=[r for r in p.readings if r.label=="Pending updates"][0]
    assert pend.value==2, "host packages must not inflate the guest count"
    assert "containers" in pend.note and "hosts" not in pend.note, pend.note


def test_no_host_row_when_hosts_are_clean():
    p=_patch_report([_t_host("pve",0),_t_guest("ct106 immich",1)])
    assert not [r for r in p.readings if r.label=="Host updates"], \
        "a clean host set should say nothing at all"


def test_an_older_push_without_kind_still_splits_correctly():
    """Payloads from a pre-2026-09-10 copy of the push script carry no `kind`."""
    p=_patch_report([{"name":"pve","count":5,"security":0,"reboot":False,"packages":[]},
                     {"name":"ct108 plex","count":2,"security":0,"reboot":False,"packages":[]}])
    pend=[r for r in p.readings if r.label=="Pending updates"][0]
    assert pend.value==2, pend
    assert [r for r in p.readings if r.label=="Host updates"][0].value==5



if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {exc!r}")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
