#!/usr/bin/env bash
# Connect a Gmail account for reading only: bills and receipts in Life admin.
# Run it yourself in SirisAI's folder on the server, so the app password stays
# with you. It tests the login first, keeps a copy of .env, and restarts SirisAI.
#
# Nothing here lets SirisAI send: no SMTP is set. Morning triage, which saves
# draft replies in Gmail, stays off unless you've already set an hour for it.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Run this where SirisAI's .env is."; exit 1; }

read -rp "Gmail address: " address
echo "App password, from myaccount.google.com/apppasswords (it won't show as you type):"
read -rs password
echo
password="${password// /}"
[ -n "$address" ] && [ -n "$password" ] || { echo "Both are needed. Nothing changed."; exit 1; }

echo "Testing the login..."
SETUP_GMAIL_ADDRESS="$address" SETUP_GMAIL_PASSWORD="$password" python3 - <<'PY' || { echo "Nothing changed."; exit 1; }
import imaplib, os, sys
try:
    c = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
    c.login(os.environ["SETUP_GMAIL_ADDRESS"], os.environ["SETUP_GMAIL_PASSWORD"])
    c.logout()
except Exception as exc:
    print(f"Gmail said no ({type(exc).__name__}). Check the address and that it's an app password, not your Google password.")
    sys.exit(1)
print("Login works.")
PY

cp -p .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
tmp="$(mktemp .env.XXXXXX)"
cat .env > "$tmp"
sed -i -E '/^SIRISAI_EMAIL_(IMAP_HOST|IMAP_PORT|USE_SSL|USERNAME|PASSWORD|FROM)=/d' "$tmp"
{
  echo
  echo "# Gmail, read-only for life admin (scripts/setup_gmail.sh)"
  echo "SIRISAI_EMAIL_IMAP_HOST=imap.gmail.com"
  echo "SIRISAI_EMAIL_IMAP_PORT=993"
  echo "SIRISAI_EMAIL_USE_SSL=true"
  echo "SIRISAI_EMAIL_USERNAME=$address"
  echo "SIRISAI_EMAIL_PASSWORD=$password"
  echo "SIRISAI_EMAIL_FROM=$address"
  grep -q '^SIRISAI_TRIAGE_HOUR_LOCAL=' "$tmp" || echo "SIRISAI_TRIAGE_HOUR_LOCAL="
} >> "$tmp"
cat "$tmp" > .env   # keeps .env's owner and mode
rm -f "$tmp"

docker compose up -d --no-build --force-recreate sirisai
echo "Done. Gmail is connected: open Life admin in SirisOS and press Check email."
