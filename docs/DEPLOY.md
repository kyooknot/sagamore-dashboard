# Deploying Sagamore

Three ways, in rough order of how quickly you'll be looking at a dashboard.
All of them end up in the same place: a config file you edit, and a restart.

Sagamore is an HTTP client with a SQLite file. One core and 512 MB is ample.

---

## Configuration comes first

However you run it, configuration works the same way, and it is layered:

| | wins over | use it for |
|---|---|---|
| **1. Environment variable** | everything | secrets, and anything you want to override per-host |
| **2. `sagamore.yaml`** | built-in defaults | the settings you actually sit down and edit |
| **3. Built-in default** | — | sensible starting points |

That ordering means you can keep every secret out of the YAML file — useful if
you ever put it in version control — while still editing the interesting parts
in one readable place.

Start from the annotated template:

```bash
cp config/sagamore.example.yaml config/sagamore.yaml
$EDITOR config/sagamore.yaml
```

Sagamore looks for the file at `$CONFIG_FILE`, then `./config/sagamore.yaml`,
then `/config/sagamore.yaml`, then `/etc/sagamore/sagamore.yaml`. The first one
that exists wins. **No file is fine too** — everything falls back to the
environment.

⚠️ A malformed YAML file is *ignored*, not fatal: the dashboard starts on
defaults rather than refusing to boot. Check `/api/health` after editing.

---

## 1. Docker

```bash
git clone https://github.com/kyooknot/sagamore-dashboard.git
cd sagamore-dashboard
mkdir -p config data
cp config/sagamore.example.yaml config/sagamore.yaml
$EDITOR config/sagamore.yaml
docker compose up -d
```

Then `http://localhost:8092`.

Two volumes matter, and only two:

- **`./config` → `/config`** — your `sagamore.yaml`. Edit on the host, then
  `docker compose restart sagamore`. No rebuild, no recreate.
- **`./data` → `/data`** — the SQLite file and cached favicons. This is the only
  state; back this up and you've backed up everything.

Secrets are better passed as environment variables than written into the YAML:

```yaml
    environment:
      - HA_TOKEN=${HA_TOKEN}
      - PVE_TOKEN_SECRET=${PVE_TOKEN_SECRET}
```

…with the values in a `.env` file beside `docker-compose.yml` (already
gitignored).

**Reaching your services.** Bridge networking is normally enough — the
container only makes outbound calls to Home Assistant, Proxmox and friends. If
those are on another VLAN, make sure the Docker host can reach them; the
container inherits that.

**Health.** The image ships a healthcheck hitting `/api/health`, so
`docker ps` shows `healthy`/`unhealthy` rather than merely "up". A dashboard
that is up but blind should not look fine — that is the whole premise of this
project applied to its own container.

---

## 2. LXC (Proxmox) or any Debian VM

Create an unprivileged Debian 13 container — 1 core, 512 MB, 4 GB disk:

```bash
pct create 127 local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst \
  --hostname sagamore --cores 1 --memory 512 --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged 1 --features nesting=1
pct start 127
pct enter 127
```

⚠️ **`features: nesting=1` is not optional on Debian 13.** Without it systemd 257
fails `dev-mqueue.mount`, `run-lock.mount` and `tmp.mount`, and the container
comes up `degraded` in ways that look like application bugs.

Then, inside the container:

```bash
apt-get update && apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/kyooknot/sagamore-dashboard/main/deploy/lxc/install.sh | bash
```

The installer creates a `sagamore` system user, a virtualenv in `/opt/sagamore`,
a hardened systemd unit, and drops a starter config at
`/etc/sagamore/sagamore.yaml`. It is idempotent — re-run it to upgrade in place,
and it will never overwrite a config file that already exists.

```bash
$EDITOR /etc/sagamore/sagamore.yaml
systemctl restart sagamore
journalctl -u sagamore -f
```

Secrets can go in `/etc/sagamore/env` (mode 600, `KEY=value` per line) instead
of the YAML file; the unit reads it, and the environment beats the file.

---

## 3. Bare systemd, no installer

If you'd rather do it yourself, the pieces are:

```bash
git clone https://github.com/kyooknot/sagamore-dashboard.git /opt/sagamore
cd /opt/sagamore
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config/sagamore.example.yaml /etc/sagamore/sagamore.yaml
cp deploy/sagamore.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now sagamore
```

`deploy/sagamore.service` runs it as a non-root user under `ProtectSystem=strict`
with `ReadWritePaths` limited to the data directory.

---

## Upgrading

| | |
|---|---|
| Docker | `git pull && docker compose up -d --build` |
| LXC / installer | re-run `deploy/lxc/install.sh` |
| Bare | `git pull && .venv/bin/pip install -r requirements.txt && systemctl restart sagamore` |

Your config and database are outside the code in every case, so none of these
touch them.

---

## Putting it behind a hostname

Sagamore speaks plain HTTP and has **no authentication** — deliberately, since
it is read-only and meant for a private network. Any reverse proxy will do for
TLS. If you expose it beyond your own network, put forward-auth in front of it,
and do that *before* adding any feature that controls something rather than
merely reporting it.

---

## Checking it works

```bash
curl -s localhost:8092/api/health | python3 -m json.tool
```

`/api/health` reports what it can and cannot currently see. Panels whose source
isn't configured say **"not configured"**; panels whose source is configured but
unreachable say **`unknown`**. Neither is rendered as healthy — that distinction
is the reason this project exists.
