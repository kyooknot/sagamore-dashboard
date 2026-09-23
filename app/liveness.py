"""Device liveness — the hard part, and the reason this project exists.

Naive freshness is wrong. `sensor.sump_pump_power` reads a constant `0 W` while
the pump is idle, and Zigbee2MQTT only republishes on change, so its
`last_updated` can be hours old while the device is perfectly healthy. Flagging
that as dead cries wolf; ignoring it is how the real dead-sensor case hid.

The workable signal with the data Home Assistant actually exposes:

    a device is as fresh as its most recently updated entity

Because most devices have at least one continuously-varying channel — a Shelly's
voltage wanders, an energy counter creeps — the max across a device's entities
tracks the device, while any single channel tracks only its own value.

Worked example, measured 2026-08-20:

    sensor.sump_pump_power    0 W      last_updated 20:20:41   <- looks stale
    sensor.sump_pump_voltage  117.4 V  last_updated 20:22:24   <- device is alive

Taking the max gives 20:22:24 and the sump reads healthy, correctly.

**Known limit, stated plainly.** If a device has *no* varying channel, this
cannot distinguish "quiet" from "gone", and neither can anything else built only
on `/api/states`. The rigorous fix is to expose Zigbee2MQTT's `last_seen`
(`last_seen: ISO_8601` in its config) as an entity, which turns liveness into a
real heartbeat. Until then a tile says "not reporting", which is a claim about
the data, never "the device is dead", which would be a claim about the world.
"""

from __future__ import annotations

import re

from .sources import parse_ha_time

# Entity-name suffixes that denote a measurement channel rather than the device.
_SUFFIXES = re.compile(
    r"_(power|energy|voltage|current|ac_frequency|temperature|humidity|battery|"
    r"linkquality|lqi(_24h)?|status|state|overheating|overpowering|overvoltage|"
    r"overcurrent|last_seen|uptime|rssi|signal_strength|wifi_signal|"
    r"countdown_to_turn_on|countdown_to_turn_off|led_brightness|"
    r"power_on_behavior|reset_total_energy|moisture)$"
)

# Devices legitimately go quiet for different lengths of time. These are
# tolerances for the *device*, not for any one reading.
DEFAULT_MAX_AGE = 3 * 60 * 60
CLASS_MAX_AGE = {
    # A Shelly plug driving a switched-OFF load publishes nothing for as long as
    # it stays off — a gym light or pool-table lamp is legitimately silent for
    # days. Crucially, the Shelly integration marks a genuinely unreachable
    # device's entities `unavailable`, which the model already treats as UNKNOWN,
    # so silence is not the death signal here and does not need a tight bound.
    "plug": 24 * 60 * 60,
    "leak": 12 * 60 * 60,      # battery sensors report rarely by design
    "climate": 2 * 60 * 60,
    "smoke": 36 * 60 * 60,     # Nest Protect checks in slowly
}


# HA's authoritative entity_id -> device_id map, installed by the slow poll.
# Empty until the first successful fetch, and empty forever if HA is unreachable,
# which is exactly when the name heuristic below has to carry the load.
_DEVICE_OF: dict[str, str] = {}


def set_device_registry(mapping: dict[str, str] | None) -> None:
    """Install Home Assistant's real entity -> device map.

    🚨 THE NAME HEURISTIC BELOW IS A GUESS, AND IT FAILS QUIETLY. Measured against
    the live registry on 2026-09-07: one Nest Protect produced **23 different keys
    for its 23 entities** -- no grouping at all -- and a Sonos produced 9 keys for
    11 entities. Each stray key is a phantom "device" of exactly one entity, which
    can never be refreshed by a sibling, so any channel sitting at a constant value
    reads as "stopped reporting" forever. That is what put a permanent
    "Primary En Suite has stopped reporting" on the Power card while the speaker
    was demonstrably alive: `_active_power` strips to `..._active`, an orphan key,
    while its 10 device-mates collapsed to `primary_en_suite`.

    `sources.unavailable_devices` already reached this conclusion in its own
    docstring -- name-prefix grouping "split one lock into five devices and merged
    unrelated ones sharing a first word" -- and used the registry instead. This
    applies the same authority to liveness.
    """
    global _DEVICE_OF
    _DEVICE_OF = dict(mapping or {})


def name_stem(entity_id: str) -> str:
    """The device's NAME as it appears inside sibling entity ids.

    `sensor.sump_pump_voltage` -> `sump_pump`
    `sensor.basementfridge_basementfridge_power` -> `basementfridge`

    🚨 THIS IS NOT `device_key`, AND THE TWO ARE NOT INTERCHANGEABLE. Use this one
    whenever the result is CONCATENATED BACK INTO AN ENTITY ID, e.g. finding a
    climate zone's matching `sensor.<stem>_power`. `device_key` returns an opaque
    Home Assistant device id once the registry is loaded, so building a name from
    it yields `sensor.<32-hex-chars>_power`, which matches nothing.

    That regression shipped on 2026-09-07: the two Samsung minisplits expose no
    `hvac_action`, so their power draw is the ONLY evidence they are idle. The
    lookup broke, both zones became "no heat/cool state", and the HVAC card went
    UNKNOWN — reporting a blind spot for a house that was simply not heating or
    cooling. Grouping wants `device_key`; string-building wants `name_stem`.
    """
    name = entity_id.split(".", 1)[1]
    prev = None
    while prev != name:                      # strip stacked suffixes
        prev = name
        name = _SUFFIXES.sub("", name)
    parts = name.split("_")
    half = len(parts) // 2
    if half and parts[:half] == parts[half:]:  # Shelly repeats the device name
        parts = parts[:half]
    return "_".join(parts)


def device_key(entity_id: str) -> str:
    """Collapse an entity_id to the device it belongs to, for GROUPING.

    Prefers Home Assistant's real device id; falls back to `name_stem` for entities
    with no device at all -- template sensors, helpers -- and for the window before
    the first registry fetch. The return value is opaque: compare it, key a dict on
    it, never build an entity id out of it (see `name_stem`).
    """
    return _DEVICE_OF.get(entity_id) or name_stem(entity_id)


def build_last_seen(states: dict) -> dict[str, float]:
    """Map device key -> epoch seconds of its most recent entity update."""
    seen: dict[str, float] = {}
    for eid, st in states.items():
        # Automations and helpers are HA-internal; they say nothing about a device.
        if eid.split(".", 1)[0] in ("automation", "script", "input_boolean",
                                    "input_number", "input_text", "input_datetime",
                                    "person", "zone", "sun", "scene"):
            continue
        ts = parse_ha_time(st.get("last_updated"))
        if ts is None:
            continue
        k = device_key(eid)
        if ts > seen.get(k, 0):
            seen[k] = ts
    return seen


def device_age(entity_id: str, last_seen: dict[str, float]) -> float | None:
    """Seconds since the *device* behind this entity was last heard from."""
    import time

    ts = last_seen.get(device_key(entity_id))
    return None if ts is None else max(0.0, time.time() - ts)
