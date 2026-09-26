#!/usr/bin/env bash
# Install Sagamore into a Debian/Ubuntu LXC container (or any bare VM).
#
# 🚧 UNVERIFIED: this script has never been run end to end. It passes `bash -n`
# and mirrors a working manual install, but it has not been executed on a fresh
# container. Read it before piping it to a shell — which is good practice for
# any curl|bash, and here it is the actual advice. Failures are expected to be
# small (a package name, a path); please open an issue with the output.
#
#   curl -fsSL https://raw.githubusercontent.com/kyooknot/sagamore-dashboard/main/deploy/lxc/install.sh | bash
# or, from a clone:
#   sudo ./deploy/lxc/install.sh
#
# Idempotent: safe to re-run to upgrade in place. It never overwrites your
# config file once it exists.
set -euo pipefail

APP_USER=${APP_USER:-sagamore}
APP_DIR=${APP_DIR:-/opt/sagamore}
CONF_DIR=${CONF_DIR:-/etc/sagamore}
DATA_DIR=${DATA_DIR:-/var/lib/sagamore}
REPO=${REPO:-https://github.com/kyooknot/sagamore-dashboard.git}
PORT=${PORT:-8092}

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo $0)"; exit 1; }

say "Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates

say "User and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$CONF_DIR" "$DATA_DIR"

say "Code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  # If we're running from inside a clone, copy it; otherwise fetch.
  if [ -f "$(dirname "$0")/../../app/main.py" ]; then
    cp -a "$(dirname "$0")/../../." "$APP_DIR/"
  else
    git clone --depth 1 "$REPO" "$APP_DIR"
  fi
fi

say "Virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "Configuration"
if [ ! -f "$CONF_DIR/sagamore.yaml" ]; then
  cp "$APP_DIR/config/sagamore.example.yaml" "$CONF_DIR/sagamore.yaml"
  echo "    wrote $CONF_DIR/sagamore.yaml — edit it before this is useful"
else
  echo "    $CONF_DIR/sagamore.yaml exists; leaving it alone"
fi
# Secrets may also live in an env file, which beats the YAML file.
[ -f "$CONF_DIR/env" ] || { : > "$CONF_DIR/env"; }
chmod 600 "$CONF_DIR/env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$CONF_DIR" "$DATA_DIR"

say "systemd unit"
cat > /etc/systemd/system/sagamore.service <<UNIT
[Unit]
Description=Sagamore — house state dashboard
Documentation=https://github.com/kyooknot/sagamore-dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=CONFIG_FILE=$CONF_DIR/sagamore.yaml
Environment=DB_PATH=$DATA_DIR/sagamore.db
EnvironmentFile=-$CONF_DIR/env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

# It reads HTTP APIs and writes one SQLite file. Nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now sagamore

say "Done"
cat <<EOM

  Config:   $CONF_DIR/sagamore.yaml      (edit, then: systemctl restart sagamore)
  Secrets:  $CONF_DIR/env                (mode 600; overrides the YAML file)
  Data:     $DATA_DIR/sagamore.db
  Logs:     journalctl -u sagamore -f
  URL:      http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT

EOM
systemctl --no-pager --lines=5 status sagamore || true
