# CLAUDE.md — Sagamore

House **state** dashboard. Read `README.md` first — the "one idea" section is the
whole design, and everything else follows from it.

## Non-negotiables

- **`unknown` is a first-class state.** Never render a value we can't currently
  observe as if it were healthy. Staleness and absence beat the caller's
  optimism (`model.Reading.effective_status`). This exists because power-loss
  alerts on a sump pump and a fridge were found to be silently unable to fire.
- **Liveness is per device, not per reading** (`liveness.py`). A constant `0 W`
  on an idle pump is not a dead sensor. Don't "simplify" this back to a single
  entity's `last_updated` — there's a test pinning it.
- **Beware truthiness on measurements.** `if volts` treats `0 V` as missing, and
  `0 V` is the most important reading a supply sensor can produce. Use
  `is not None`. There's a test for this too, because it was a real bug.
- **Collectors never raise.** One dead source degrades one panel.
- **Never fetch secrets at runtime.** Read them from the environment at start.
  A secrets store must never be a runtime dependency of the dashboard.
- **No build step, no CDN, system fonts.** The dashboard must render when the
  house has no internet.
- **Read-only.** It reads Home Assistant and Proxmox; it does not control them.
  Adding any control action means adding authentication first.

## Conventions

- Python 3.11+, `httpx`, a virtualenv alongside the code.
- Runs as a dedicated non-root user under systemd; environment file mode 600.
- State lives in SQLite at `DB_PATH`.

## Testing

```bash
python -m pytest -q          # 243 tests, no network, no configuration needed
```

Panel builders are pure functions of collected data — keep them that way, it's
why they're testable. `tests/conftest.py` points `DB_PATH` at a temporary
directory so a fresh clone works without setup.

Before claiming a panel works, run it against a real instance: `poll_fast()`
then `build_panels()`. Doing that is what surfaced a range-hood filter at 7% life
and an expired trash-calendar anchor — neither was visible in any test.

## Gotchas already paid for

- Home Assistant's `last_updated` resets to the restart time for **every** entity
  on an HA restart, so anything that hasn't changed since looks freshly stale.
  Device-level liveness plus a tolerance window absorbs this.
- Some integrations repeat the device name inside the entity id
  (`basementfridge_basementfridge_power`); `device_key()` and `_friendly()` both
  collapse it.
- Entity ids cannot contain an apostrophe, so `friendly_name` is the better
  source for some devices and the id is better for others. `_friendly()` picks
  between them, and the tests document each case.
- A media server can mint a `media_player` per connecting client and mark it
  unavailable the moment it disconnects. `HA_IGNORE_DEVICES` takes patterns, not
  just exact names, for exactly this reason.
