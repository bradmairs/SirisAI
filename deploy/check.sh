#!/usr/bin/env bash
# Are both apps up, and can each reach the other with the keys from .env?
set -uo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
ok=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; ok=1; }
ai="http://localhost:${SIRISAI_PORT:-8000}"; os="http://localhost:${SIRISOS_PORT:-8094}"

curl -fsS "$ai/health" >/dev/null && pass "SirisAI is up ($ai)" || fail "SirisAI isn't answering on $ai (make logs s=sirisai)"
curl -fsS "$os/health" >/dev/null && pass "SirisOS is up ($os)" || fail "SirisOS isn't answering on $os (make logs s=sirisos)"

# SirisOS -> SirisAI: SirisOS's own view of the SirisAI tile.
state=$(docker compose exec -T sirisos python -c "
import urllib.request, json, os
r = urllib.request.Request('http://sirisai:8000/siris/hub/v1', headers={'Authorization': 'Bearer ' + os.environ['SIRISAI_API_KEY']})
print(json.load(urllib.request.urlopen(r, timeout=5))['contract'])" 2>&1)
[ "$state" = "1" ] && pass "SirisOS reaches SirisAI's hub contract (v1)" || fail "SirisOS can't reach SirisAI: $state"

# SirisAI -> SirisOS: the service key opens the calculators.
calc=$(docker compose exec -T sirisai python -c "
import urllib.request, json, os
r = urllib.request.Request('http://sirisos:6464/api/v1/engineering/calculators', headers={'Authorization': 'Bearer ' + os.environ['SIRISAI_SIRISOS_SERVICE_KEY']})
print(len(json.load(urllib.request.urlopen(r, timeout=5))['calculators']))" 2>&1)
[[ "$calc" =~ ^[0-9]+$ ]] && pass "SirisAI reaches SirisOS's engineering library ($calc calculators)" || fail "SirisAI can't use SirisOS's service key: $calc"

llm=$(curl -fsS -H "Authorization: Bearer $SIRISAI_API_KEY" "$ai/siris/status" 2>/dev/null | python3 -c "import sys, json; print(json.load(sys.stdin).get('llm_provider'))" 2>/dev/null)
[ -n "$llm" ] && [ "$llm" != "none" ] && pass "SirisAI has a model ($llm)" || fail "SirisAI has no model yet: set SIRISAI_LLM_PROVIDER in sirisai.env, then make restart"
exit $ok
