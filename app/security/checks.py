"""The individual checks behind the security briefing
(app/skills/builtin/security.py). Each returns plain data and never raises
for an unreachable target -- one dead host shouldn't blank the briefing.
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
from datetime import UTC, datetime
from typing import Any

from app.cron.client import CronHost, CronHostError, CronHostsClient

# --- TLS certificates


def _parse_cert(der: bytes) -> dict[str, Any]:
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        not_after = cert.not_valid_after_utc
        subject = cert.subject.rfc4514_string()
        issuer = cert.issuer.rfc4514_string()
    except ImportError:
        # The stdlib can decode a PEM file (CPython's own test helper, stable
        # for years) -- used when `cryptography` isn't installed.
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
            path = f.name
        try:
            decoded = ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]
        finally:
            os.unlink(path)
        not_after = datetime.strptime(decoded["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        subject = ", ".join("=".join(part) for rdn in decoded.get("subject", ()) for part in rdn)
        issuer = ", ".join("=".join(part) for rdn in decoded.get("issuer", ()) for part in rdn)
    return {"not_after": not_after, "subject": subject, "issuer": issuer}


def check_certificate(target: str, timeout: float = 5.0, now: datetime | None = None) -> dict[str, Any]:
    """Days until `host[:port]`'s certificate expires. Verification is off on
    purpose: homelab certificates are often self-signed, and the question
    here is when it expires, not whether it's trusted."""
    host, _, port = target.partition(":")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, int(port or 443)), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        info = _parse_cert(der)
    except (OSError, ValueError, ssl.SSLError) as exc:
        return {"target": target, "error": f"{type(exc).__name__}: {exc}"[:160]}
    days = (info["not_after"] - (now or datetime.now(UTC))).total_seconds() / 86400
    return {"target": target, "expires": info["not_after"].isoformat(), "days_left": round(days, 1), "subject": info["subject"], "issuer": info["issuer"]}


# --- failed SSH logins on the cron hosts

_FAIL_PATTERN = "Failed password|Invalid user|authentication failure"
# The journal if it has any matches, else /var/log/auth.log -- journalctl can
# exist and exit 0 with nothing in it (found live: sshd logging elsewhere), so
# "journalctl || auth.log" never reached the file.
_FAILED_LOGINS_COMMAND = (
    "out=$(journalctl -u ssh -u sshd --since '{hours} hours ago' --no-pager -q 2>/dev/null | grep -E '" + _FAIL_PATTERN + "'); "
    "[ -n \"$out\" ] || out=$(tail -n 20000 /var/log/auth.log 2>/dev/null | grep -E '" + _FAIL_PATTERN + "'); "
    "printf '%s\\n' \"$out\" | grep -oE 'from [0-9a-fA-F:.]+' | sort | uniq -c | sort -rn | head -5; echo __done__"
)


def failed_ssh_logins(client: CronHostsClient, hours: int = 24) -> list[dict[str, Any]]:
    results = []
    for host in client.hosts:
        try:
            output = client._run(host, _FAILED_LOGINS_COMMAND.format(hours=int(hours)))
        except CronHostError as exc:
            results.append({"host": host.label, "error": str(exc)[:160]})
            continue
        sources = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0].isdigit() and parts[1] == "from":
                sources.append({"ip": parts[2], "attempts": int(parts[0])})
        results.append({"host": host.label, "failed": sum(s["attempts"] for s in sources), "top_sources": sources})
    return results


# --- container image vulnerabilities (Trivy)


def trivy_available(binary: str = "trivy") -> bool:
    return shutil.which(binary) is not None


def scan_image(image: str, binary: str = "trivy", timeout: float = 600.0) -> dict[str, Any]:
    """HIGH/CRITICAL vulnerabilities in one image, counted, with the worst few."""
    try:
        result = subprocess.run(
            [binary, "image", "--quiet", "--format", "json", "--scanners", "vuln", "--severity", "HIGH,CRITICAL", image],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"image": image, "error": f"{type(exc).__name__}: {exc}"[:160]}
    if result.returncode != 0:
        return {"image": image, "error": (result.stderr.strip() or "trivy failed")[-200:]}
    report = json.loads(result.stdout or "{}")
    vulns = [v for r in report.get("Results") or [] for v in r.get("Vulnerabilities") or []]
    counts = {"CRITICAL": 0, "HIGH": 0}
    for v in vulns:
        if v.get("Severity") in counts:
            counts[v["Severity"]] += 1
    worst = sorted(vulns, key=lambda v: (v.get("Severity") != "CRITICAL", v.get("VulnerabilityID", "")))[:3]
    return {
        "image": image,
        "critical": counts["CRITICAL"],
        "high": counts["HIGH"],
        "fixable": sum(1 for v in vulns if v.get("FixedVersion")),
        "examples": [f"{v.get('VulnerabilityID')} in {v.get('PkgName')} {v.get('InstalledVersion')}" for v in worst],
    }


def hosts_from(client: CronHostsClient | None) -> list[CronHost]:
    return client.hosts if client is not None else []
