# Sagamore — build plan

## Why not just configure Homepage harder

Homepage is a **launcher**. Its widgets poll a service and print a number, and
that is genuinely all it is designed to do. Three things it structurally cannot
do, all of which are the actual pain:

1. **It holds no state.** Every widget is instantaneous, so "are we on pace to
   beat last month's bill" and "has this sensor gone quiet" are both
   unexpressible — they need history.
2. **It has no concept of a stale reading.** A widget bound to a dead sensor
   prints the last value in the same styling as a live one. This is not
   hypothetical: the 2026-08 audit found the sump-pump and basement-fridge
   power-loss alerts could not fire, because Zigbee served retained values and
   everything downstream looked healthy.
3. **It has no judgement.** It shows `47` where what you want is *"47 is fine"*
   or *"47 is the problem"*. Encoding that in YAML means encoding thresholds in
   YAML, which is where config formats go to die.

So: keep Homepage's one genuinely good idea (the bookmark grid), and build the
state layer properly.

---

## Phase 1 — the state core ✅ built

- Four-state model (`ok` / `warn` / `alert` / **`unknown`**) where staleness and
  absence always beat optimism.
- Device-level liveness (`liveness.py`) — max `last_updated` across a device's
  entities, because a single channel is constant on a healthy device.
- Collectors that never raise, so one dead integration degrades one panel.
- Panels: Power, Leak Watch, Security, HVAC, Trash, Homelab Exposure.
- Bookmarks: local store, browser-export import, quick-add endpoint.
- SQLite series table so trends become possible.
- 14 tests, no network required.

**Found on the very first live run**, which is the argument for the whole
approach: the range-hood grease filter is at **7 % life**, the trash calendar
anchor **expired 2026-06-30**, and power is pacing **22 % over** a typical month.
None of those were visible anywhere before.

## Open decision — host patch debt needs a write permission

The Homelab panel can read nodes, guests, storage and backup tasks with a
**read-only** token (`PVEAuditor`). It cannot read *pending updates*:

```
GET /nodes/{node}/apt/update  ->  403  Permission check failed (/nodes/pve, Sys.Modify)
```

Proxmox classes listing pending packages as part of the update *workflow*, so it
demands `Sys.Modify` — a genuine write permission that would also let the token
change node network config. **I did not grant it**: a read-only dashboard should
not hold cluster-write rights, and quietly widening a token to make a tile go
green is the wrong trade.

The panel therefore renders `unknown` for that row and names the cause. Three
ways forward, in the order I'd recommend them:

1. **Drop host patch-debt from this dashboard and link to the 08:00 digest.**
   `update-manager.sh` already covers hosts *and* every CT *and* the Mac,
   Windows PC, HA, UniFi and Sonos — far more than this could. Duplicating it
   badly is worse than pointing at it.
2. **Read the digest's own output.** `update-manager.sh` writes state under
   `/var/lib/update-manager/` on `pve`. Exposing that as a tiny read-only JSON
   endpoint gives Sagamore the *full* picture with no new privilege.
   Best result, small amount of work.
3. **Grant `Sys.Modify`.** Fastest, and the one I'd argue against.

Until this is decided the tile is honestly blank rather than falsely green —
which is the entire point of the project.

## Phase 2 — make the history earn its keep

- **Sparklines** per circuit from the `series` table (24 h / 7 d), rendered as
  inline SVG — no chart library, no build step.
- **Daily kWh per circuit**, derived from the energy counters rather than
  integrating watts (counters survive restarts; integration doesn't).
- **Phantom-load view** — non-baseline circuits idling above a few watts. The
  data is already collected and flagged; it just needs a panel.
- **"Unusual for this hour"** — compare a circuit against its own trailing
  median for that hour-of-week. This is the cheapest real anomaly detection and
  needs no model.

## Phase 3 — close the sensing gaps

Ranked by what they'd actually change:

| Gap | Fix | Why it matters |
|---|---|---|
| **No whole-house energy** | Emporia Vue 2 (~$120, 16 circuits) or a CT clamp on the main | Today the utility integration is the only whole-house number and it updates once a day. 12 measured circuits is a small share of the real load. |
| **No true device heartbeat** | Expose Zigbee2MQTT `last_seen: ISO_8601` as entities | Turns liveness from a good inference into a fact, and closes audit P1 #5 properly |
| **No camera/NVR state** | UniFi Protect API collector | Security panel currently sees doors and smoke, not cameras |
| **Trash anchor expired** | Re-anchor from the 2026-27 town calendar | The panel already flags itself as unverified — honest, but it wants fixing |
| **No water metering** | Flume or a pulse sensor on the main | Leak Watch detects *puddles*, not a slow leak inside a wall |

## Phase 4 — from dashboard to instrument

- **Explain-on-tap**: every tile links to *why* it says what it says — the
  entities behind it, their ages, the threshold applied. A dashboard you can
  interrogate is one you'll still trust in a year.
- **Change feed**: "what changed in the house today", from the `event` table.
- **Weekly digest** to Discord, reusing the existing webhook, so the dashboard
  pushes as well as pulls.
- **Correlation**: HVAC runtime against outdoor temperature; power against
  occupancy. Cheap to compute once the series table has a few weeks in it.

---

## Deployment

**A small container or VM is ample** — this is an HTTP client with a SQLite
file, so one core and 512 MB is plenty. Run it unprivileged, as a dedicated
non-root user under systemd, with its environment in a mode-600 file. The
supplied unit in `deploy/sagamore.service` does all of that.

Put a reverse proxy in front for TLS if you want a hostname. Give it a **new**
hostname rather than replacing whatever dashboard you already run — that way the
old one keeps working untouched until this has earned the swap.

If you reach your network over a VPN, nothing extra is needed: the dashboard is
plain HTTP behind your proxy, and it neither knows nor cares how you arrived.

### Trying it first

It will happily run alongside an existing dashboard on any host that already has
Python — a virtualenv, a port and an environment file. Removing it again is
`systemctl disable --now sagamore` and deleting the directory; nothing else on
the machine is touched.

---

## Decisions worth recording

1. **Patch debt is pushed, not pulled.** Reading pending updates from the
   Proxmox API needs `Sys.Modify`, a write permission a read-only dashboard
   should not hold. A push covers hosts *and* every container, which the API
   never could.
2. **The bookmark store is whatever you already run.** It began on linkding,
   moved to Linkwarden for Floccus support, and ended on Nextcloud Bookmarks
   because Nextcloud was already running — removing a service rather than
   adding one. The panel only needs a URL and a token.
3. **Still open — whole-house energy monitoring.** The single highest-value
   hardware addition, and what would make the Power panel complete rather than
   indicative. Per-circuit monitors measure a small share of real load.

### Still to decide

- **Does this replace your existing dashboard?** Run both for a fortnight and
  see which one you actually open.
- **Should it sit behind authentication?** Today it has none: it is read-only
  and expected to live on a private network. That stops being a defensible
  position the moment it gains a control action.
