# Sagamore

A **state** dashboard for a house — not a link grid.

> Named for **Sagamore Hill**, Theodore Roosevelt's house at Oyster Bay — the
> "Summer White House", from which he ran the country for seven summers. The
> seat of government wherever he happened to be, which is what a dashboard you
> read from anywhere is for. Roosevelt was also a compulsive recorder: a trained
> naturalist who kept meticulous species and bird lists, wrote some thirty-five
> books and an estimated 150,000 letters. He would have kept a dashboard.

Most home dashboards answer *"where do I click?"*. Sagamore answers **"what is
the house doing, and can I still see it?"**

It reads Home Assistant, Proxmox, UniFi and a few optional sources, and renders
server-side. No build step, no CDN, no JavaScript framework — it must render
when the house has no internet.

---

## The one idea

Every tile resolves to **four** states, not two:

| | meaning |
|---|---|
| `ok` | fine, and we know that recently |
| `warn` | wants attention |
| `alert` | wrong right now |
| **`unknown`** | **we cannot currently see it** — never rendered as fine |

That fourth state is the whole point.

The project started after an audit found that power-loss alerts on a sump pump
and a basement fridge **could not fire**: Zigbee kept serving the last retained
value, so every dashboard kept showing a healthy wattage for a sensor that was
gone. A dashboard that renders a dead sensor as green is worse than no
dashboard, because it manufactures confidence.

So a reading carries **when it was last actually observed**, and staleness always
beats the caller's optimism (`app/model.py`).

### Liveness is a property of the device, not the reading

The naive version of this cries wolf. A sump-pump power sensor reads a constant
`0 W` while the pump is idle, and Zigbee2MQTT only republishes on change — so
that entity's `last_updated` is legitimately hours old on a perfectly healthy
pump:

```
sensor.sump_pump_power    0 W      last_updated 20:20:41   <- looks stale
sensor.sump_pump_voltage  117.4 V  last_updated 20:22:24   <- device is alive
```

So liveness is computed per **device** — the newest `last_updated` across every
entity belonging to it (`app/liveness.py`). Most devices have at least one
continuously varying channel; a plug's voltage wanders even when its load is flat.

**Known limit, stated plainly:** a device with *no* varying channel can't be
distinguished from a quiet one, and nothing built only on `/api/states` can do
better. The rigorous fix is to expose Zigbee2MQTT's `last_seen` as an entity,
which turns this into a real heartbeat. Until then a tile says *"not reporting"*
— a claim about the data — never *"the device is dead"*, which would be a claim
about the world.

---

## Panels

| Panel | Source | What it tells you |
|---|---|---|
| **Power** | Per-circuit energy monitors + a utility integration | Live draw per circuit, each circuit's **movement against its own recent normal**, and month-to-date **on pace vs a typical month** |
| **Leak Watch** | Leak sensors, sump plug, water-heater fault flags | Wet/dry *and* whether each sensor is still talking |
| **Security** | Garage doors, smoke/CO alarms, device trackers, people | What's open, what's alarming, who's on the network |
| **HVAC** | Thermostats, room temps, filter/service dates | What's running now, and what maintenance is coming due |
| **Trash** | Pure date logic, no network | Next pickup, which stream, holiday-shifted |
| **Homelab Exposure** | Proxmox API + HA `update.*` + TLS | Patch debt, backup freshness, cert expiry, guests down |
| **Now Playing** | Sonos · Apple TV · Xbox · PlayStation | Only what's actually on — idle devices hidden entirely |
| **Consoles** | HA Xbox integration + PlayStation DDP | Power state, storage headroom, firmware where it exists |
| **Gaming Progress** | Optional hourly export | Separate scoreboards, never summed |
| **Game Collection** | Optional GameVault instance | What's on the shelf, its value, what's wanted |
| **Storage & Shares** | Proxmox storage API + pushed ZFS snapshot | Pool headroom and what's eating it |
| **Bookmarks** | Nextcloud Bookmarks | The link grid, kept — just no longer the main event |

Every panel degrades on its own. A dead source yields `unknown` for that panel,
never a blank page or a stack trace.

### Now Playing — hiding the idle

A house with twenty-one media players that lists them all to say "off"
seventeen times buries the one that matters. The panel shows only what's in use
and says "Nothing playing" otherwise.

Two judgements make it useful rather than noisy:

- **A speaker group is one row.** Four speakers on one track is one thing
  happening; `group_members` collapses them to `Basement +2`.
- **Paused since yesterday is not "in use".** A pause counts for two hours —
  after that the device is idle in every sense that matters. Measured on real
  hardware: four speakers sat `paused` with a track loaded, but
  `media_position_updated_at` was the previous day's restart. Anything keyed on
  `state == "paused"` would have claimed music was playing.

Speakers, streamers and consoles all present as `media_player.*` with nothing
saying which is which, and the ids don't help — `media_player.basement` may be a
streamer while `media_player.basement_2` is a speaker. The integration registry
is the only reliable source, read on a slow loop.

### What the consoles will and won't tell you

Verified against real hardware, and they invert each other exactly:

| | firmware | storage |
|---|---|---|
| **Xbox Series X** | ✗ nothing exposes it | ✓ via the Xbox Live cloud HA already uses |
| **PlayStation 5** | ✓ local DDP `system-version` | ✗ nothing exposes it |

The PS5's entire DDP response is seven fields, and in rest mode it opens no TCP
port at all. Each gap is stated on the tile as a documented fact rather than
left as an empty space — such readings are marked `informational` and excluded
from the panel's roll-up, because something established as unobtainable is
knowledge, not a blind spot.

### "vs usual" — why the gates are where they are

Knowing the bill is 20% up doesn't tell you what to go switch off. Each circuit
is compared against **itself**: mean watts over the last 24 hours against mean
watts over the fortnight before.

Three deliberate refusals, all to stop the column crying wolf:

- **The mean, not the median or a spot reading.** A dishwasher sits at 0 W most
  of the day. Its median is 0, so any comparison is division by nothing, and a
  spot reading only says whether you looked mid-cycle. A mean over a fixed
  window is proportional to energy, which is what "using more than usual" means.
- **Two gates, not one.** A circuit is flagged only if it moves ≥15% *and* ≥5 W.
  Percent alone shouts about a phone charger going 3 W → 6 W; watts alone shout
  about a 2 kW dryer wobbling by 30 W.
- **No percentage against an idle baseline.** If the fortnight average is under
  the watt gate, report the change in watts instead. The first live run produced
  *"Dishwasher +4193%"* — arithmetically perfect, informationally worthless. It
  now reads `▲ +45 W`.

Circuits without enough history say **learning** rather than inventing a normal
they cannot yet know.

---

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/env.example .env.local && $EDITOR .env.local     # fill in tokens
set -a; . ./.env.local; set +a
.venv/bin/uvicorn app.main:app --reload --port 8092
```

Tests need no network and no configuration — every panel builder is a pure
function of collected data:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q          # 252 tests
```

They need no configuration either: `tests/conftest.py` points the database at a
temporary directory, so a fresh clone runs green.

### Configuration

Two ways, and you can mix them. **An optional YAML file** —
[`config/sagamore.example.yaml`](config/sagamore.example.yaml), copied to
`config/sagamore.yaml` — holds the settings you actually sit down and edit, in
the spirit of Homepage's `services.yaml`: edit the file, restart, done. **And
environment variables**, which always win over the file, so secrets can stay out
of it entirely.

Precedence is: explicit environment variable → YAML file → built-in default. A
malformed YAML file is ignored rather than fatal — the dashboard starts on
defaults instead of refusing to boot.

`deploy/env.example` lists the same settings in environment form.

| Variable | Purpose |
|---|---|
| `HA_URL`, `HA_TOKEN` | Home Assistant — required for every house panel |
| `PVE_HOST`, `PVE_TOKEN_ID`, `PVE_TOKEN_SECRET`, `PVE_NODES` | Proxmox — a **read-only** token is enough (`PVEAuditor` on `/`) |
| `UNIFI_HOST`, `UNIFI_API_KEY`, `UNIFI_SITE`, `UNIFI_GUEST_NETWORKS` | UniFi — optional; the panel degrades cleanly without it |
| `NEXTCLOUD_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_PASSWORD`, `NEXTCLOUD_INBOX` | Bookmarks store. Use an **app password**, never the account password |
| `GAMEVAULT_URL`, `LINKDING_URL`, `LINKDING_TOKEN` | Optional extras |
| `FAVORITES` | Quick-link strip: `"Name\|url\|icon-slug"`, comma separated. Icon slugs from [dashboard-icons](https://github.com/homarr-labs/dashboard-icons) |
| `HA_IGNORE_DEVICES` | Patterns for ephemeral entities (e.g. a media server minting a player per client) |
| `DB_PATH`, `TZ`, `CERT_HOST`, `INGEST_TOKEN` | Storage location, timezone, cert to watch, push auth |

Secrets are read from the environment at **start** time, not fetched at runtime,
so a secrets store being unreachable can never take the dashboard down.

### Deploying

Full instructions for all three routes are in **[docs/DEPLOY.md](docs/DEPLOY.md)**.
The short version:

**Docker** — the quickest way to look at it:

```bash
mkdir -p config data
cp config/sagamore.example.yaml config/sagamore.yaml && $EDITOR config/sagamore.yaml
docker compose up -d
```

`./config` and `./data` are the only mounts that matter: edit the config on the
host and `docker compose restart`, and back up `./data` to back up everything.

**LXC / Debian VM** — an unprivileged container, 1 core and 512 MB:

```bash
curl -fsSL https://raw.githubusercontent.com/kyooknot/sagamore-dashboard/main/deploy/lxc/install.sh | bash
```

That creates a system user, a virtualenv, a hardened systemd unit and a starter
config at `/etc/sagamore/sagamore.yaml`. It is idempotent — re-run it to upgrade,
and it will never overwrite an existing config.

**Bare systemd** — [`deploy/sagamore.service`](deploy/sagamore.service) runs it as
a non-root user with `ProtectSystem=strict` and `ReadWritePaths` limited to the
data directory.

Put a reverse proxy in front of it for TLS if you want a hostname.

⚠️ **Debian 13 containers need `features: nesting=1`** under LXC, or systemd 257
fails `dev-mqueue.mount`, `run-lock.mount` and `tmp.mount` and the container
comes up `degraded`.

### Data that arrives by push, not pull

Two things are pushed *in* rather than polled, and both are deliberate:

**Patch debt.** `GET /nodes/{node}/apt/update` requires **`Sys.Modify`** — a
write permission a read-only dashboard has no business holding, and it only
covers hosts anyway. So a script on the hypervisor posts to `/api/ingest/patch`
instead, covering hosts *and* every container. Example scripts are in
[`deploy/pve/`](deploy/pve/).

**Self-hosted app versions.** A package scan cannot see apps that are git
checkouts, release tarballs, Docker tags or source builds. Working out whether
each is current means holding **every app's API key**, which Sagamore
deliberately does not. Whatever already computes that for you can post it to
`/api/ingest/apps`.

An update is not news until it needs a person: each one carries `applies` —
`tonight` (an automated run will apply it), `self` (the app updates itself) or
`you`. Only `you` warns immediately; the others turn into a warning only on
evidence that the automated run missed them, or after a 26-hour backstop.

An empty report is refused and treated as `unknown`. *"All 0 apps current"* is a
blind spot wearing an all-clear, and this project exists not to do that.

---

## Design rules

- **One failing source degrades one panel.** Collectors never raise.
- **Never fetch secrets at runtime.** Start-time only.
- **No build step.** Server-rendered Jinja plus a little vanilla JS, system
  fonts, no CDN — it must render when the house has no internet.
- **SQLite, not Postgres.** The whole dataset is a few MB a year.
- **Read-only by default.** It reads Home Assistant and Proxmox; it does not
  control them. Adding control means adding authentication first.

## Security posture

There is **no authentication** in the app, deliberately: it is read-only and
intended to sit on a private network or behind a VPN, not on the public
internet. If you expose it, put forward-auth in front of it — and if you add any
control action, do that *before* you add the action, not after.

The bookmark capture endpoint accepts `GET` as well as `POST`, because a
bookmarklet cannot `POST` cross-origin without CORS. That is a mutation behind a
`GET`, which is only acceptable because the endpoint is private and purely
additive. Treat it accordingly.

## Further reading

- [`docs/PLAN.md`](docs/PLAN.md) — why this exists rather than configuring an
  existing dashboard harder, and what's planned next
- [`docs/bookmarks.md`](docs/bookmarks.md) — the bookmark design, including a
  detailed account of why Floccus cannot work on iOS
- [`CLAUDE.md`](CLAUDE.md) — the non-negotiables, for anyone (or any agent)
  changing this code
