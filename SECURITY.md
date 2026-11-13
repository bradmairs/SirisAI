# Security

SirisAI runs on a home network and can control the house, servers and
accounts it's connected to, so problems are taken seriously.

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's
private reporting instead: **Security → Report a vulnerability** on this
repository. Include what you found, how to reproduce it, and what it lets
someone do.

## Running it safely

- Always set `SIRISAI_API_KEY` (the Docker deployment's `setup.sh` generates
  one). Without it, only the machine SirisAI runs on can connect.
- Don't expose SirisAI directly to the internet. Reach it over a VPN
  (WireGuard, Tailscale) or behind a reverse proxy that authenticates.
- Use a long random ntfy topic, or a self-hosted ntfy, and set
  `SIRISAI_NTFY_TOKEN`: anyone who knows a topic on ntfy.sh can read it.
  Push buttons carry a one-request token, never the API key.
- Set an approval PIN (`SIRISAI_APPROVAL_PIN`) so big actions (reboots,
  unlocking, installs) need it.
- Keep secrets in `.env`, which git ignores. Never commit it.
