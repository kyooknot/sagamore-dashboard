#!/bin/bash
# Push ZFS dataset usage to Sagamore.
#
# WHY PUSH: the Proxmox API exposes pool health and vdev layout but NOT per-dataset usage
# (/nodes/<n>/disks/zfs/<pool> returns state/errors/scan/children — the vdev tree, not the
# filesystems). Per-project numbers only come from `zfs list`, which needs a shell on the
# storage host. Same pattern as the patch and app pushes.
#
# Read-only: `zfs list` mutates nothing.
#
# Installed at /usr/local/bin/sagamore-storage-push.sh on pve.
set -uo pipefail

ENDPOINT="${SAGAMORE_URL:-http://192.168.1.88:8092}/api/ingest/storage"
TOKEN_FILE=/etc/sagamore-push.token
[ -r "$TOKEN_FILE" ] || { echo "missing $TOKEN_FILE" >&2; exit 1; }
TOKEN=$(cat "$TOKEN_FILE")

TMP=$(mktemp /tmp/sagamore-storage.XXXXXX)
trap 'rm -f "$TMP"' EXIT

# -p = exact bytes (no "6.15T" to re-parse). Skip the zvols and container subvols; they are
# guest disks, not shares, and they would swamp the list.
#
# `usedbydataset` is what the dataset holds ITSELF, excluding children. Reporting `used`
# alone double-counted every parent: tank/immich and tank/immich/data both read
# 77 GB, because the parent's `used` INCLUDES the child's.
zfs list -Hp -o name,used,usedbydataset,avail,mountpoint 2>/dev/null \
  | grep -vE 'subvol-|/vm-|basevol-' > "$TMP" || true

[ -s "$TMP" ] || { echo "zfs list produced nothing — not posting" >&2; exit 1; }

# ---- plain directories at a pool root -----------------------------------------
# ZFS cannot break these out: /bulk/romfleet is an ordinary directory, not a
# dataset, so 2.6 TB of ROM library showed up only as the pool root's own usage and the
# card reported RomFleet as 6 GB (which is the small `roms` dataset next to it).
#
# `du` here walks millions of files, so it is CACHED and refreshed at most every 12h —
# hourly would be genuinely abusive. -x stops it crossing into child datasets, which are
# already covered above. nice/ionice keep it off the media services' backs.
DIRCACHE=/var/lib/sagamore-storage-dirs.tsv
if [ ! -s "$DIRCACHE" ] || [ "$(( $(date +%s) - $(stat -c %Y "$DIRCACHE" 2>/dev/null || echo 0) ))" -gt 43200 ]; then
  : > "$DIRCACHE.new"
  for root in $(zfs list -Hp -o name,mountpoint | awk -F'\t' '$1 !~ /\// && $2 != "-" {print $2}'); do
    nice -n19 ionice -c3 du -x --max-depth=1 -B1 "$root" 2>/dev/null \
      | awk -v r="$root" -F'\t' '$2 != r {print $2"\t"$1}' >> "$DIRCACHE.new"
  done
  [ -s "$DIRCACHE.new" ] && mv "$DIRCACHE.new" "$DIRCACHE" || rm -f "$DIRCACHE.new"
fi

ENDPOINT="$ENDPOINT" TOKEN="$TOKEN" DIRCACHE="$DIRCACHE" python3 - "$TMP" <<'PY'
import json, os, sys, urllib.request

rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 4:
        continue
    name, used, self_used, avail, mnt = parts[0], parts[1], parts[2], parts[3], parts[4]
    try:
        rows.append({"name": name, "used": int(used), "self": int(self_used),
                     "avail": int(avail), "mount": mnt})
    except ValueError:
        continue

if not rows:
    sys.exit("no datasets parsed — not posting")

dirs = []
try:
    for line in open(os.environ.get("DIRCACHE", ""), encoding="utf-8", errors="replace"):
        pth, _, size = line.rstrip("\n").partition("\t")
        try:
            dirs.append({"path": pth, "used": int(size)})
        except ValueError:
            continue
except OSError:
    pass

body = json.dumps({"source": os.uname().nodename, "datasets": rows, "dirs": dirs}).encode()
req = urllib.request.Request(os.environ["ENDPOINT"], data=body, method="POST")
req.add_header("Authorization", "Bearer " + os.environ["TOKEN"])
req.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"pushed {len(rows)} datasets + {len(dirs)} dirs -> {r.status}")
except Exception as exc:
    sys.exit(f"push failed: {exc}")
PY
