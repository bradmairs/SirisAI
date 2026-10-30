"""Certificates and domains that are about to expire, in the attention feed
(daily at SIRISAI_EXPIRY_CHECK_HOUR_LOCAL, 9am).

- **Certificates**: each `host[:port]` in SIRISAI_SECURITY_CERT_HOSTS, plus
  port 443 on every SSH server (SIRISAI_EXPIRY_CHECK_SERVERS, on). A server
  that doesn't serve HTTPS is skipped quietly; a listed host that can't be
  reached is raised.
- **Domains** (SIRISAI_SECURITY_DOMAINS): the registration's expiry date,
  from the registry's RDAP service (via rdap.org, which redirects to the
  right one; no key needed).

Raised SIRISAI_SECURITY_CERT_WARN_DAYS (14) ahead, urgent inside a week.
Items close themselves once renewed. The security briefing still lists
certificates too.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from app.hub.attention import action
from app.security.checks import check_certificate

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

RDAP = "https://rdap.org/domain/{}"


def domain_expiry(domain: str, now: datetime | None = None, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    domain = domain.strip().lower().rstrip(".")
    try:
        with httpx.Client(timeout=15, follow_redirects=True, transport=transport, headers={"Accept": "application/rdap+json"}) as client:
            response = client.get(RDAP.format(domain))
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"domain": domain, "error": f"{type(exc).__name__}: {exc}"[:200]}
    expires = next((e.get("eventDate") for e in data.get("events") or [] if e.get("eventAction") == "expiration"), None)
    if not expires:
        return {"domain": domain, "error": "the registry doesn't publish an expiry date"}
    when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    days = (when - (now or datetime.now(UTC))).total_seconds() / 86400
    return {"domain": domain, "expires": when.isoformat(), "days_left": round(days, 1)}


def _cert_targets(siris: "SirisAI") -> tuple[list[str], set[str]]:
    listed = siris.settings.security_cert_host_list()
    auto: set[str] = set()
    if siris.settings.expiry_check_servers and siris.ssh_client is not None:
        for host in siris.ssh_client.hosts:
            target = f"{host.host}:443"
            if target not in listed and host.host not in listed:
                auto.add(target)
    return [*listed, *sorted(auto)], auto


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    warn = siris.settings.security_cert_warn_days
    targets, auto = _cert_targets(siris)
    domains = [d.strip() for d in siris.settings.security_domains.split(",") if d.strip()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        certs = list(pool.map(lambda t: check_certificate(t, now=now), targets))
        regs = list(pool.map(lambda d: domain_expiry(d, now), domains))
    wanted: list[dict[str, Any]] = []
    for cert in certs:
        target = cert["target"]
        if "error" in cert:
            if target not in auto:
                wanted.append(_item(f"expiry:cert-error:{target}", f"Couldn't check the certificate on {target}", cert["error"], "attention"))
        elif cert["days_left"] < warn:
            wanted.append(
                _item(
                    f"expiry:cert:{target}",
                    _title(f"The certificate on {target}", cert["days_left"]),
                    f"Expires {cert['expires'][:10]}. Issued by {cert['issuer'] or 'unknown'}. Renew it, or check that automatic renewal is running.",
                    "urgent" if cert["days_left"] < 7 else "attention",
                )
            )
    for reg in regs:
        if "error" in reg:
            wanted.append(_item(f"expiry:domain-error:{reg['domain']}", f"Couldn't check when {reg['domain']} expires", reg["error"], "info"))
        elif reg["days_left"] < warn:
            wanted.append(
                _item(
                    f"expiry:domain:{reg['domain']}",
                    _title(f"The domain {reg['domain']}", reg["days_left"]),
                    f"Its registration expires {reg['expires'][:10]}. Renew it with your registrar (or check auto-renew and the card on file).",
                    "urgent" if reg["days_left"] < 7 else "attention",
                )
            )
    siris.attention.sync("expiry", wanted, outcome="Renewed")
    return {"certificates": [c for c in certs if not ("error" in c and c["target"] in auto)], "domains": regs}


def _title(what: str, days: float) -> str:
    if days < 0:
        return f"{what} has expired"
    whole = int(days)
    return f"{what} expires {'today' if whole == 0 else 'tomorrow' if whole == 1 else f'in {whole} days'}"


def _item(key: str, title: str, body: str, severity: str) -> dict[str, Any]:
    return {"key": key, "kind": "alert", "title": title, "body": body, "severity": severity, "actions": [action("acknowledge", "Got it")]}
