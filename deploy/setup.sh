#!/usr/bin/env bash
# One-time (and safe to re-run) setup for the SirisAI + SirisOS stack:
#   1. .env with fresh secrets, from .env.example
#   2. sirisai.env and sirisos.env, from each app's own .env.example, with
#      the settings this stack wires up itself commented out
#   3. the data folders
#   4. the SirisOS checkout (cloned next to SirisAI if missing); the
#      integration (SirisOS ADR 110) is in its main branch
# It never overwrites a file you've already edited.
set -euo pipefail
cd "$(dirname "$0")"
here="$(pwd)"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
secret() { openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# 1. .env ---------------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env"
fi
for key in SIRISAI_API_KEY SIRIS_SERVICE_KEY SIRISOS_JWT_SECRET; do
  if grep -qE "^${key}=$" .env; then
    value="$(secret)"
    sed -i.bak "s|^${key}=$|${key}=${value}|" .env && rm -f .env.bak
    say "Generated ${key}"
  fi
done
chmod 600 .env
set -a; . ./.env; set +a

# 4 (first, since step 2 reads its .env.example). The SirisOS checkout -----------------
sirisos="$(cd "$here" && realpath -m "${SIRISOS_SRC:-../../SirisOS}")"
if [ ! -d "$sirisos/.git" ]; then
  say "Cloning SirisOS into $sirisos"
  git clone https://github.com/bradmairs/SirisOS.git "$sirisos"
fi
# The SirisAI integration (SirisOS ADR 110) is in SirisOS main now. Earlier
# versions of this script applied it as a patch on a local branch called
# sirisai-integration; a checkout still on that branch should go back to main.
if [ "$(git -C "$sirisos" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "sirisai-integration" ]; then
  say "SirisOS at $sirisos is on the old 'sirisai-integration' branch. The integration is in SirisOS main now:"
  say "  git -C $sirisos checkout main && git -C $sirisos pull"
fi

# 2. App settings ----------------------------------------------------------------
# Lines for settings the compose file sets are commented out, so editing them
# here can't silently disagree with the wiring.
from_example() {  # <example> <output> <managed keys regex> <header>
  local example="$1" out="$2" managed="$3" header="$4"
  [ -f "$out" ] && return 0
  { printf '%s\n\n' "$header"; sed -E "s/^(${managed})=/# set by docker-compose.yml: \1=/" "$example"; } > "$out"
  chmod 600 "$out"
  say "Created $out"
}

from_example ../.env.example sirisai.env \
  'SIRISAI_(DATABASE_URL|API_KEY|SIRISOS_URL|SIRISOS_PUBLIC_URL|SIRISOS_SERVICE_KEY|HOST|PORT|TIMEZONE)' \
"# SirisAI's settings, for this stack (generated from SirisAI's .env.example).
# Start with: SIRISAI_LLM_PROVIDER=ollama and SIRISAI_OLLAMA_BASE_URL -- not
# localhost (that's this container): http://host.docker.internal:11434 when
# Ollama runs on this server, else its LAN address. Then Home Assistant,
# weather and ntfy. Every other setting below is optional.
# Apply changes with: make restart"

from_example "$sirisos/.env.example" sirisos.env \
  'SIRISAI_(URL|API_KEY|PUBLIC_URL)|SIRISOS_(SERVICE_KEY|JWT_SECRET|PORT|TIMEZONE|STANDARDS_PATH)' \
"# SirisOS's settings, for this stack (generated from SirisOS's .env.example).
# Set SIRISOS_ADMIN_PASSWORD first, then the apps SirisOS connects to.
# Apply changes with: make restart"

# 3. Data ------------------------------------------------------------------------
mkdir -p data/sirisai/ssh data/sirisos/app data/sirisos/standards data/sirisos/logs data/caddy
chmod 700 data/sirisai/ssh

say "Done. Next:"
echo "  1. Edit sirisai.env (model, Home Assistant...) and sirisos.env (admin password, apps)."
echo "  2. make up          # build and start both"
echo "  3. make check       # both healthy and talking to each other"
echo "  SirisAI: http://${SIRIS_HOST:-localhost}:${SIRISAI_PORT:-8000}   SirisOS: http://${SIRIS_HOST:-localhost}:${SIRISOS_PORT:-8094}"
