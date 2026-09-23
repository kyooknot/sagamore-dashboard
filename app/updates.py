"""When is a pending update something a human needs to hear about?

2026-09-11, Alex: "Getting notice of updates throughout the day, then only updating at
night creates alerts that can't be acted upon." The pushers report every few hours; the
05:00 routine run on pve applies what it can. An update that will apply itself tonight
is therefore not news. It becomes news in exactly two cases:

  * it needs a human anyway -- gated: a critical-service package, a reboot, a major
    version, an app nothing updates automatically; or
  * it SHOULD have applied itself and did not.

The second is judged from evidence, not from the clock: the update was already pending
when the last completed 05:00 run STARTED, and it is still pending in a report taken
after that run FINISHED. Both halves matter. Without the first, an update that appeared
at 05:30 would be blamed on a run that never saw it. Without the second, the app report
pushed at 00:35 -- before the run -- still lists the update at 05:30, and would blame the
run for something it had just fixed. Nothing re-pushes the app report after the run, so
that window is real and lasts until the next push.

A backstop of a day and a bit catches what the evidence rule cannot: a 05:00 run that
never happens at all (timer disabled, pve down) leaves no newer run to compare against.
"""
from __future__ import annotations

BACKSTOP = 26 * 3600

# How long a pause is remembered after the player stops reporting it (see
# carry_pause_since). Long enough to cover a device that is powered off nightly and woken
# each morning still holding the same frozen frame; short enough that a record cannot
# outlive any plausible resumption of the same media.
RETAIN_PAUSE = 7 * 86400


def missed_run(first_seen, observed_at, routine, now, *, runs_nightly=True) -> bool:
    """True when an update that should have applied itself plainly has not.

    `routine` is {"last_start", "last_end"} for the last COMPLETED 05:00 run, as sent by
    pve's update-routine-status. `runs_nightly=False` is for apps with their own updater
    (the servarr trio), which do not work to the 05:00 clock: only the backstop applies.
    """
    if not first_seen:
        return False
    r = routine or {}
    start, end = r.get("last_start"), r.get("last_end")
    if runs_nightly and start and end and first_seen < start and observed_at > end:
        return True
    return now - first_seen > BACKSTOP


def carry_pause_since(prev, paused_now, now, seed=None):
    """Remember when each paused media player was FIRST seen paused on this media.

    Home Assistant's own timestamps cannot date a pause. A restart or an integration
    reload rewrites `last_changed` AND `media_position_updated_at` for every entity: on
    2026-09-12 HA restarted at 14:49:53 and all 2112 entities were restamped, so an Apple
    TV paused since 05:03 the previous morning looked 15 minutes old. It had held the Now
    Playing card for a day and a half, because the 20-minute pause grace was measured
    against a clock that kept resetting.

    `paused_now` maps entity_id -> an identity string for what is loaded (title and
    position). When that identity changes the pause is a NEW one, so the clock restarts:
    someone pausing a different episode is activity, the same frozen frame is not.

    `seed` maps entity_id -> HA's own best timestamp for the pause, used ONLY for a pause
    we have no record of. Without it, the first poll after a deploy would stamp every
    existing pause as starting *now* and hand each one a fresh grace period — worse than
    the HA timestamp it replaced. A genuinely new pause is unaffected, because HA's
    timestamp is then about `now` anyway; `min` keeps a restamped clock from pushing a
    pause into the future.
    """
    out = {}
    for eid, ident in (paused_now or {}).items():
        p = (prev or {}).get(eid)
        keep = (isinstance(p, dict) and p.get("ident") == ident and p.get("since"))
        if keep:
            since = p["since"]
        else:
            s = (seed or {}).get(eid)
            since = min(now, s) if isinstance(s, (int, float)) else now
        # No `last_seen` while it is paused: the entry must stay byte-identical across
        # polls, or main.py would write the snapshot every single poll.
        out[eid] = {"ident": ident, "since": since}

    # A pause is NOT forgotten when the player stops reporting it. The Office Apple TV is
    # turned on by an HA automation every morning and whenever the alarm is disarmed; it
    # wakes showing the SAME frozen Plex episode (same title, same position, untouched
    # since 09-11). Deleting the record on the off state meant every wake looked like a
    # brand-new pause and took the card for another 20 minutes -- twice in three days.
    # Keep it, and restore its clock if the player comes back paused on the same thing.
    # Anything a human actually did moves the position, which changes `ident` and starts a
    # new clock, so this cannot hide real activity.
    for eid, p in (prev or {}).items():
        if eid in out or not isinstance(p, dict) or not p.get("since"):
            continue
        last = p.get("last_seen") or p.get("since")
        if now - last <= RETAIN_PAUSE:          # else: stale, drop it and stop carrying it
            out[eid] = {"ident": p.get("ident"), "since": p["since"], "last_seen": last}
    return out


def carry_patch_first_seen(prev_targets, targets, now):
    """Stamp each pending package with when it was first reported, keeping the stamp from
    the previous push for any package still pending. Mutates and returns `targets`."""
    seen = {}
    for t in prev_targets or []:
        if isinstance(t, dict):
            for pkg, ts in (t.get("first_seen") or {}).items():
                seen[(t.get("name"), pkg)] = ts
    for t in targets:
        if isinstance(t, dict):
            t["first_seen"] = {pkg: seen.get((t.get("name"), pkg), now)
                               for pkg in (t.get("packages") or [])}
    return targets


def carry_app_first_seen(prev_apps, apps, now):
    """The same for apps, keyed on the version on offer. A newer release is a new update:
    keyed on the name alone, 3.2.1 landing an hour after the run applied 3.2.0 would
    inherit 3.2.0's stamp and read as a run that failed. Mutates and returns `apps`."""
    seen = {(a.get("name"), a.get("latest") or ""): a.get("first_seen")
            for a in prev_apps or []
            if isinstance(a, dict) and a.get("state") == "update" and a.get("first_seen")}
    for a in apps:
        if isinstance(a, dict) and a.get("state") == "update":
            a["first_seen"] = seen.get((a.get("name"), a.get("latest") or "")) or now
    return apps
