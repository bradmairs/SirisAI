#!/usr/bin/env bash
# The updater: applies an update SirisAI asked for, and rolls it back if the
# new version doesn't come up healthy. SirisAI runs in a container and can't
# rebuild itself, so it writes data/sirisai/update/request.json and this
# script, run every minute by cron on the server, does the rest.
#
#   ./updater.sh once        process a waiting request, if there is one
#   ./updater.sh install     add the every-minute cron entry (make auto-update)
#   ./updater.sh uninstall   remove it
#
# An update: refuse if either checkout has local edits; fast-forward SirisAI
# to the release tag and pull SirisOS; rebuild and restart; then check both
# apps answer, SirisAI reports the new version, and its tool-selection eval
# is no worse than before. Any failure puts both checkouts back, rebuilds,
# and says why in status.json, which SirisAI reads and reports.
set -uo pipefail
cd "$(dirname "$0")"
DEPLOY=$(pwd)
[ -f .env ] && { set -a; . ./.env; set +a; }

UPDATE_DIR=${UPDATE_DIR:-$DEPLOY/data/sirisai/update}
AI_SRC=${AI_SRC:-$(cd .. && pwd)}
OS_SRC=${SIRISOS_SRC:-../../SirisOS}
case "$OS_SRC" in /*) ;; *) OS_SRC="$DEPLOY/$OS_SRC" ;; esac
COMPOSE=${COMPOSE:-docker compose}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-180}
AI_URL=${AI_URL:-http://localhost:${SIRISAI_PORT:-8000}}
OS_URL=${OS_URL:-http://localhost:${SIRISOS_PORT:-8094}}
REQ="$UPDATE_DIR/request.json"
WORK="$UPDATE_DIR/request.processing.json"
STATUS="$UPDATE_DIR/status.json"
LOG="$UPDATE_DIR/update.log"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG"; }

status() {  # status <state> <detail>
  python3 - "$STATUS" "$1" "$2" "${target:-}" "${from:-}" "$LOG" <<'PY'
import json, sys, datetime
path, state, detail, version, from_version, log = sys.argv[1:]
try:
    tail = "".join(open(log).readlines()[-40:])
except OSError:
    tail = ""
out = {"state": state, "detail": detail, "version": version, "from_version": from_version, "log_tail": tail}
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
out["finished_at" if state != "running" else "started_at"] = now
json.dump(out, open(path, "w"), indent=2)
PY
}

field() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }

ai_get() { curl -fsS --max-time 10 -H "Authorization: Bearer ${SIRISAI_API_KEY:-}" "$@"; }

healthy() {  # healthy <expected version>: wait for both apps, SirisAI on that version
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT )) version
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS --max-time 5 "$AI_URL/health" >/dev/null 2>&1 && curl -fsS --max-time 5 "$OS_URL/health" >/dev/null 2>&1; then
      version=$(ai_get "$AI_URL/siris/status" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null)
      [ -z "$1" ] || [ "$version" = "$1" ] && return 0
    fi
    sleep "${HEALTH_POLL:-5}"
  done
  return 1
}

rebuild() { $COMPOSE build >>"$LOG" 2>&1 && $COMPOSE up -d >>"$LOG" 2>&1; }

rollback() {  # rollback <why>
  log "rolling back: $1"
  git -C "$AI_SRC" reset -q --hard "$old_ai" >>"$LOG" 2>&1
  [ -n "${old_os:-}" ] && git -C "$OS_SRC" reset -q --hard "$old_os" >>"$LOG" 2>&1
  if rebuild && healthy "$from"; then
    status rolled_back "$1; rolled back to $from"
  else
    status failed "$1, and the rollback to $from didn't come back healthy either"
  fi
  finish
}

finish() { rm -f "$WORK"; rmdir "$UPDATE_DIR/.lock" 2>/dev/null; exit 0; }

once() {
  [ -f "$REQ" ] || exit 0
  mkdir "$UPDATE_DIR/.lock" 2>/dev/null || exit 0  # another run is on it
  mv "$REQ" "$WORK"
  target=$(field "$WORK" version); from=$(field "$WORK" from_version); baseline=$(field "$WORK" baseline_selection_accuracy)
  log "update requested: $from -> $target"
  status running "updating to $target"

  for src in "$AI_SRC" "$OS_SRC"; do
    if [ -d "$src/.git" ] && [ -n "$(git -C "$src" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
      status skipped "$src has local edits, so I left it alone. Commit or stash them, then ask again"; finish
    fi
  done
  old_ai=$(git -C "$AI_SRC" rev-parse HEAD)
  old_os=""; [ -d "$OS_SRC/.git" ] && old_os=$(git -C "$OS_SRC" rev-parse HEAD)

  git -C "$AI_SRC" fetch -q --tags origin >>"$LOG" 2>&1 || { status skipped "couldn't fetch from GitHub"; finish; }
  git -C "$AI_SRC" rev-parse -q --verify "refs/tags/v$target" >/dev/null || { status skipped "there's no v$target tag"; finish; }
  git -C "$AI_SRC" merge -q --ff-only "v$target" >>"$LOG" 2>&1 || { status skipped "v$target isn't a fast-forward from this checkout (local commits?)"; finish; }
  if [ -n "$old_os" ]; then git -C "$OS_SRC" pull -q --ff-only >>"$LOG" 2>&1 || log "SirisOS didn't pull (kept as it was)"; fi

  rebuild || rollback "the build failed"
  healthy "$target" || rollback "SirisAI $target didn't come up healthy within ${HEALTH_TIMEOUT}s"
  if [ -n "$baseline" ]; then
    accuracy=$(ai_get -X POST "$AI_URL/siris/evals/run?selection_only=true" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["selection"]["accuracy"])' 2>/dev/null)
    [ -n "$accuracy" ] || rollback "the selection eval didn't run on $target"
    python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)' "$accuracy" "$baseline" \
      || rollback "tool selection got worse on $target ($baseline% -> $accuracy%)"
  fi
  log "updated to $target"
  status updated "${accuracy:+tool selection $accuracy%}"
  finish
}

# The newest release tag, through git on the server (4.5): it already has access to the repo, so SirisAI can see a
# new release of a private repo without a GitHub token. At most once an hour.
latest_tag() {
  local out="$UPDATE_DIR/latest.json" tag
  [ -f "$out" ] && [ -z "$(find "$out" -mmin +55 2>/dev/null)" ] && return 0
  tag=$(git -C "$AI_SRC" ls-remote --tags --refs origin 'v*' 2>/dev/null | awk -F/ '{print $3}' | sed 's/^v//' \
        | python3 -c 'import sys; from decimal import Decimal as D
vs = [v.strip() for v in sys.stdin if v.strip()]
ok = []
for v in vs:
    try: ok.append((D(v), v))
    except Exception: pass
print(max(ok)[1] if ok else "")' 2>/dev/null)
  [ -n "$tag" ] && printf '{"version": "%s", "checked_at": "%s", "via": "git"}\n' "$tag" "$(date -u +%FT%TZ)" >"$out"
}

case "${1:-once}" in
  # The heartbeat tells SirisAI (and SirisOS's Update button) the updater is installed and running.
  once) mkdir -p "$UPDATE_DIR"; date -u +%FT%TZ >"$UPDATE_DIR/heartbeat"; latest_tag; once ;;
  install)
    mkdir -p "$UPDATE_DIR"
    ( crontab -l 2>/dev/null | grep -v '# siris-updater'; echo "* * * * * $DEPLOY/updater.sh once >/dev/null 2>&1 # siris-updater" ) | crontab -
    echo "Installed: the updater runs every minute (crontab -l to see it)." ;;
  uninstall)
    crontab -l 2>/dev/null | grep -v '# siris-updater' | crontab -
    echo "Removed the updater's cron entry." ;;
  *) echo "usage: $0 once|install|uninstall" >&2; exit 2 ;;
esac
