#!/bin/bash
# Push a self-hosted-app version report to Sagamore.
#
# WHY PUSH: working out whether Sonarr, Immich or Authentik has an update means
# holding each app's API key. Sagamore deliberately holds none of them. pve
# already computes exactly this every morning for the Discord digest's
# "Self-hosted apps" section, so it reports its findings in — the same pattern
# as sagamore-patch-push.sh, the Mac, and the Windows PC.
#
# This is a SEPARATE script and a SEPARATE endpoint from the patch push on
# purpose: the two have different sources and different failure modes, so one
# going stale must not take the other's data with it.
#
# ⚠️ app-update.sh is ALWAYS invoked with --dry-run. Without it the script
# actually updates things. A dashboard collector must never mutate the estate.
#
# Installed at /usr/local/bin/sagamore-app-push.sh on pve.
set -uo pipefail

ENDPOINT="${SAGAMORE_URL:-http://192.168.1.88:8092}/api/ingest/apps"
TOKEN_FILE=/etc/sagamore-push.token
APP_UPDATE="${APP_UPDATE:-/usr/local/bin/app-update.sh}"

[ -r "$TOKEN_FILE" ]  || { echo "missing $TOKEN_FILE" >&2; exit 1; }
[ -x "$APP_UPDATE" ]  || { echo "missing $APP_UPDATE" >&2; exit 1; }
TOKEN=$(cat "$TOKEN_FILE")

# --dry-run is not optional here. See the warning above.
OUT=$("$APP_UPDATE" --dry-run 2>/dev/null)

# An empty or unparseable run must NOT post an empty list: Sagamore would render
# "all 0 current", i.e. a blind spot dressed up as an all-clear. Posting nothing
# lets the existing snapshot age out to `unknown`, which is the honest answer.
if [ -z "$OUT" ]; then
  echo "app-update.sh produced no output — not posting" >&2; exit 1
fi

# When the last completed 05:00 run started and finished (added 2026-09-11). Sagamore
# needs it to tell an update that applies tonight from one a run failed to apply.
ROUTINE=$(/usr/local/bin/update-routine-status 2>/dev/null)

# ⚠️ The data goes in via a FILE, not a pipe. `python3 - <<'PY'` makes the
# heredoc python's stdin (that is where the program comes from), which silently
# overrides anything piped in — sys.stdin.read() then returns "". Same trap as
# `ssh host bash -s <<EOF` swallowing a pipe. Cost: one confusing empty run.
TMP=$(mktemp /tmp/sagamore-app-push.XXXXXX)
trap 'rm -f "$TMP"' EXIT
printf '%s\n' "$OUT" > "$TMP"

ENDPOINT="$ENDPOINT" TOKEN="$TOKEN" ROUTINE="$ROUTINE" python3 - "$TMP" <<'PY'
import json, os, re, sys, urllib.request

# app-update.sh marks every line with exactly one of these.
MARK = {"✅": "ok",       # OK
        "\U0001f535": "update",  # blue circle
        "⚠": "unknown",  # warning
        "↩": "failed",   # rolled back
        "⛔": "failed"}   # no entry / unhealthy

LINE = re.compile(r"^\s*[•\-*]\s*(?P<name>[^—]+?)\s*—\s*(?P<rest>.+?)\s*$")
VERS = re.compile(r"(?P<a>\d[\w.+-]*)\s*(?:->|→)\s*(?P<b>\d[\w.+-]*)")
CUR  = re.compile(r"current\s*\(\s*(?P<v>[^,)\s]+)")

apps = []
for raw in open(sys.argv[1], encoding="utf-8").read().splitlines():
    m = LINE.match(raw)
    if not m:
        continue
    name, rest = m.group("name").strip(), m.group("rest").strip()
    state = None
    for ch, st in MARK.items():
        if ch in rest:
            state = st
            break
    if state is None:                 # a line we don't recognise is a blind spot,
        state = "unknown"             # never an all-clear
    # strip the marker (and any variation selector) from the human text
    detail = rest
    for ch in MARK:
        detail = detail.replace(ch + "️", "").replace(ch, "")
    detail = detail.strip(" -—")

    cur = lat = ""
    v = VERS.search(rest)
    if v:
        cur, lat = v.group("a"), v.group("b")
    else:
        c = CUR.search(rest)
        if c:
            cur = c.group("v")

    low = rest.lower()
    managed = ("held" if "held" in low else
               "manual" if "manual" in low else
               "apt-auto" if "apt-auto" in low or "apt —" in low else
               "self" if "self-updating" in low else
               "auto" if "auto" in low else "")

    # Who clears this update (added 2026-09-11), read off app-update.sh's own wording:
    #   tonight — the 05:00 run applies it ("would auto-update", "auto-applies")
    #   self    — the app's built-in updater, on its own clock (servarr "auto-installs")
    #   you     — anything else: gated, held, manual. Unrecognised wording lands here on
    #             purpose, so a new handler can only ever warn too much, never hide one.
    applies = ""
    if state == "update":
        applies = ("you" if "held" in low else
                   "tonight" if ("would auto-update" in low or "auto-applies" in low) else
                   "self" if "auto-installs" in low else "you")

    apps.append({"name": name, "state": state, "current": cur, "latest": lat,
                 "detail": detail, "managed": managed, "held": "HELD" in rest,
                 "applies": applies})

if not apps:
    sys.exit("no app lines parsed — not posting")

try:
    routine = json.loads(os.environ.get("ROUTINE") or "null")
except ValueError:
    routine = None

body = json.dumps({"source": os.uname().nodename, "routine": routine, "apps": apps}).encode()
req = urllib.request.Request(os.environ["ENDPOINT"], data=body, method="POST")
req.add_header("Authorization", "Bearer " + os.environ["TOKEN"])
req.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"pushed {len(apps)} apps -> {r.status}")
except Exception as exc:
    sys.exit(f"push failed: {exc}")
PY
