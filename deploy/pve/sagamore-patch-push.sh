#!/bin/bash
# Push a patch-debt report to Sagamore.
#
# WHY PUSH: reading pending updates through the Proxmox API needs Sys.Modify -
# a genuine write permission a read-only dashboard should not hold. pve already
# knows the answer for the hosts AND every container, so it reports in. This is
# the same push pattern the Mac and the Windows PC already use for the 08:00
# digest.
#
# This deliberately does NOT run `apt-get update`. Refreshing the package cache
# in ~30 containers on a timer would be genuinely abusive; the cache is already
# refreshed daily by update-manager.sh and the apt-daily timers. We only read
# what is already known.
#
# Installed at /usr/local/bin/sagamore-patch-push.sh on pve.
set -uo pipefail

ENDPOINT="${SAGAMORE_URL:-http://192.168.1.88:8092}/api/ingest/patch"
TOKEN_FILE=/etc/sagamore-push.token
[ -r "$TOKEN_FILE" ] || { echo "missing $TOKEN_FILE" >&2; exit 1; }
TOKEN=$(cat "$TOKEN_FILE")

# Tier rules come from update-manager.sh itself, so the two can never disagree about which
# guest updates wait for Alex. READ, never sourced: sourcing that script runs it.
UM=/usr/local/bin/update-manager.sh
CRIT=$(sed -n "s/^CRIT='\([^']*\)'.*/\1/p" "$UM" 2>/dev/null)
HOLDS=$(sed -n 's/^\(HOLD\|NOAPPLY_CT\)="\([^"]*\)".*/\2/p' "$UM" 2>/dev/null | tr '\n' ' ')

json_escape() { python3 -c 'import json,sys;print(json.dumps(sys.stdin.read().strip()))'; }

targets=""
add_target() {  # name, count, security, reboot, packages(csv), kind(host|guest)
  # `kind` added 2026-09-10. A host and a guest pending the same number of packages mean
  # completely different things: a guest is cleared by the 05:00 routine run, so anything
  # above zero means the automation is broken; a host is Tier H and can ONLY be cleared by
  # a planned rolling reboot, so a count there is scheduled work, not a fault. Summing them
  # produced a warning every single morning that nothing could act on, which is how a card
  # stops being read. Inferring it from the name downstream would work today and break the
  # day a container gets named "pve-something".
  local pkgs kind="${6:-guest}" gated=false why="" id
  # `gated` added 2026-09-11: would update-manager HOLD this guest's updates for approval
  # rather than apply them at 05:00? Sagamore warns about those straight away and stays
  # quiet about the rest until a 05:00 run has had its chance. Same rule update-manager
  # uses: a critical-service package, a pending reboot, or a held CT. If the rules could
  # not be read, say so and gate it -- never assume a guest is routine.
  if [ "$kind" = guest ] && [ "${2:-0}" -gt 0 ]; then
    if [ -z "$CRIT" ]; then
      why="tier rules unreadable"
    else
      [ "$4" = true ] && why="needs a reboot"
      # here-string, not a pipe: under pipefail an early `grep -q` exit SIGPIPEs the
      # writer and the whole test reads as false
      grep -qiE "$CRIT" <<<"${5//,/$'\n'}" && why="${why:+$why, }critical-service package"
      id=${1#ct}; id=${id%% *}
      case " $HOLDS " in *" $id "*) why="${why:+$why, }held" ;; esac
    fi
    [ -n "$why" ] && gated=true
  fi
  # 60, not 12: Sagamore stamps each package with when it was first seen, and a package
  # cut from the list would lose its stamp. The card still shows only 12.
  pkgs=$(printf '%s' "$5" | python3 -c '
import json,sys
raw=[p for p in sys.stdin.read().split(",") if p]
print(json.dumps(raw[:60]))')
  targets="${targets}{\"name\":\"$1\",\"count\":$2,\"security\":$3,\"reboot\":$4,\"kind\":\"$kind\",\"gated\":$gated,\"gated_why\":\"$why\",\"packages\":$pkgs},"
}

# The probe, as ONE string. It must stay a single argument all the way to the
# remote shell: `ssh host sh -c '<script>'` re-joins argv with spaces, so the
# remote ends up running bare `apt` with the rest as positional parameters -
# which prints apt's HELP TEXT, and grepping that for "/" yields lines like
# "full-upgrade - upgrade the system by removing". Ask how I know.
PROBE='apt list --upgradable 2>/dev/null | grep "/" ; [ -f /var/run/reboot-required ] && echo REBOOT-REQUIRED'

scan() {  # name, mode(local|ssh|pct), target
  local name="$1" mode="$2" target="${3:-}"
  local out count sec reboot pkgs
  case "$mode" in
    local) out=$(sh -c "$PROBE" 2>/dev/null) ;;
    ssh)   out=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" "$PROBE" 2>/dev/null) ;;
    pct)   out=$(pct exec "$target" -- sh -c "$PROBE" 2>/dev/null) ;;
  esac
  [ -z "$out" ] && { add_target "$name" 0 0 false "" "${KIND:-guest}"; return; }
  reboot=false
  grep -q "REBOOT-REQUIRED" <<<"$out" && reboot=true
  pkgs=$(grep "/" <<<"$out" | cut -d/ -f1 | paste -sd, -)
  count=$(grep -c "/" <<<"$out")
  sec=$(grep "/" <<<"$out" | grep -ci security)
  add_target "$name" "${count:-0}" "${sec:-0}" "$reboot" "$pkgs" "${KIND:-guest}"
}

# --- cluster hosts ---
for node in pve pve3 pve4; do
  KIND=host
  if [ "$node" = "$(hostname)" ]; then
    scan "$node" local
  else
    scan "$node" ssh "$node"
  fi
  unset KIND
done

# --- every running container on EVERY node ---
# This used to walk `pct list` on the node the script happens to run on, which
# is pve. pve3 has no containers so nothing was lost there, but pve4's six were
# omitted entirely - and the panel still captioned the total "28 hosts +
# containers", so an incomplete count read as full coverage. On 2026-08-23 that
# hid 5 pending packages across CT204/205/206.
#
# The remote half is fed to `bash -s` over STDIN rather than passed as argv.
# `ssh host sh -c '<script>'` re-joins argv with spaces, which is how this
# script once ended up running bare `apt` and parsing its help text into
# phantom package names. A quoted heredoc also means nothing is expanded
# locally - the remote receives it verbatim.
for node in pve pve3 pve4; do
  if [ "$node" = "$(hostname)" ]; then
    for id in $(pct list 2>/dev/null | awk 'NR>1 && $2=="running"{print $1}'); do
      name=$(pct config "$id" 2>/dev/null | awk -F': ' '/^hostname:/{print $2}')
      scan "ct${id} ${name:-}" pct "$id"
    done
    continue
  fi
  # One ssh per NODE, not per container - six round trips per node on a timer
  # would be silly. Emits one tab-separated line per running container.
  remote_out=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$node" 'bash -s' 2>/dev/null <<'REMOTE'
PROBE='apt list --upgradable 2>/dev/null | grep "/" ; [ -f /var/run/reboot-required ] && echo REBOOT-REQUIRED'
for id in $(pct list 2>/dev/null | awk 'NR>1 && $2=="running"{print $1}'); do
  name=$(pct config "$id" 2>/dev/null | awk -F': ' '/^hostname:/{print $2}')
  out=$(pct exec "$id" -- sh -c "$PROBE" 2>/dev/null)
  reboot=false
  printf '%s' "$out" | grep -q REBOOT-REQUIRED && reboot=true
  pkgs=$(printf '%s' "$out" | grep "/" | cut -d/ -f1 | paste -sd, -)
  count=$(printf '%s' "$out" | grep -c "/")
  sec=$(printf '%s' "$out" | grep "/" | grep -ci security)
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "${name:-}" "${count:-0}" "${sec:-0}" "$reboot" "$pkgs"
done
REMOTE
)
  # A node that is down must not silently vanish from the report - that is the
  # same blind spot in a different costume. It reports as unreachable instead.
  if [ -z "$remote_out" ]; then
    if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$node" true 2>/dev/null; then
      echo "WARN: $node unreachable - its containers are NOT in this report" >&2
      add_target "${node} containers UNREACHABLE" 0 0 false "" host
    fi
    continue
  fi
  while IFS=$'\t' read -r id name count sec reboot pkgs; do
    [ -n "$id" ] || continue
    add_target "ct${id} ${name}" "${count:-0}" "${sec:-0}" "${reboot:-false}" "$pkgs" guest
  done <<< "$remote_out"
done

# When the last completed 05:00 run started and finished (added 2026-09-11), so Sagamore
# can tell "applies tonight" from "a run came and went without applying it".
routine=$(/usr/local/bin/update-routine-status 2>/dev/null); [ -n "$routine" ] || routine=null
payload="{\"source\":\"$(hostname)\",\"routine\":$routine,\"targets\":[${targets%,}]}"
code=$(curl -s -o /tmp/sagamore-push.out -w '%{http_code}' -m 30 -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$payload")
if [ "$code" = "200" ]; then
  echo "pushed $(grep -o '"targets":[0-9]*' /tmp/sagamore-push.out) to $ENDPOINT"
else
  echo "push failed: HTTP $code $(head -c 200 /tmp/sagamore-push.out)" >&2
  exit 1
fi
