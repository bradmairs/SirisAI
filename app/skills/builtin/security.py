"""Security briefing and backup check -- one place that answers "is anything
about the homelab's security or backups wrong?".

security_briefing gathers: TLS certificates close to expiry
(SIRISAI_SECURITY_CERT_HOSTS), failed SSH logins on the cron hosts (milestone
104's SSH access), UniFi intrusion-prevention alerts, the last Trivy scan of
running container images (security_scan_images, weekly on the scheduler when
`trivy` is installed), and the backup check. Every section is best-effort: an
unreachable source shows up as "unavailable", never as a failed briefing.

The scheduler runs it every SIRISAI_SECURITY_CHECK_INTERVAL_HOURS and acts
only on problems: an urgent push for a failed/stale backup or a certificate
inside a week of expiry, a queued note (for the daily briefing) for the rest.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.security import backups, checks
from app.skills.base import Skill

if TYPE_CHECKING:
    from app.core.siris import SirisAI

TRIVY_KEY = "trivy_scan"


def run_backup_check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    client = siris._synology_client
    if client is None:
        return {"available": False, "tasks": [], "warnings": []}
    tasks = client.get_backup_tasks()
    detailed = []
    for task in tasks:
        status = client.get_backup_task_status(task.get("task_id")) if task.get("task_id") is not None else {}
        detailed.append({**task, **status})
    zone = clock.get_zone(siris.settings.timezone)
    evaluated = backups.evaluate(detailed, now or datetime.now(UTC), siris.settings.backup_max_age_hours, zone)
    warnings = [f"Backup '{t['name']}' needs attention: {t['problem']}" for t in evaluated if not t["ok"]]
    return {"available": True, "tasks": evaluated, "warnings": warnings}


class BackupCheckSkill(Skill):
    name = "backup_check"
    description = (
        "Check that the NAS's Hyper Backup tasks actually succeeded recently -- not just that they exist. "
        "Flags failed runs and backups that haven't succeeded within the expected window."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        return run_backup_check(siris)


class SecurityScanImagesSkill(Skill):
    name = "security_scan_images"
    description = (
        "Scan the images of running Docker containers for HIGH/CRITICAL vulnerabilities with Trivy. Slow (minutes); "
        "the result is kept for the security briefing."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", **kwargs: Any) -> Any:
        if siris._docker_client is None:
            return {"available": False, "reason": "Docker isn't configured."}
        if not checks.trivy_available():
            return {"available": False, "reason": "trivy isn't installed where SirisAI runs."}
        images = sorted({c["image"] for c in siris._docker_client.list_containers(all_containers=False) if c.get("image")})
        results = [checks.scan_image(image) for image in images]
        scan = {"scanned_at": datetime.now(UTC).isoformat(), "images": results}
        siris.state.set(TRIVY_KEY, scan)
        return scan


class SecurityBriefingSkill(Skill):
    name = "security_briefing"
    description = (
        "A security and backup check-up of the homelab: certificates about to expire, failed SSH logins, "
        "UniFi intrusion alerts, vulnerable container images, and whether backups succeeded."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, siris: "SirisAI", now: datetime | None = None, **kwargs: Any) -> Any:
        settings = siris.settings
        warnings: list[str] = []
        urgent: list[str] = []
        report: dict[str, Any] = {}

        certs = [checks.check_certificate(target, now=now) for target in settings.security_cert_host_list()]
        report["certificates"] = certs
        for cert in certs:
            if "error" in cert:
                warnings.append(f"Couldn't check the certificate on {cert['target']}: {cert['error']}")
            elif cert["days_left"] < settings.security_cert_warn_days:
                line = f"The certificate on {cert['target']} expires in {max(cert['days_left'], 0):.0f} days"
                (urgent if cert["days_left"] < 7 else warnings).append(line)

        if siris.cron_client is not None:
            logins = checks.failed_ssh_logins(siris.cron_client)
            report["ssh_failed_logins"] = logins
            for host in logins:
                if host.get("failed", 0) >= settings.security_ssh_failed_threshold:
                    top = ", ".join(f"{s['ip']} ({s['attempts']})" for s in host["top_sources"][:3])
                    warnings.append(f"{host['failed']} failed SSH logins on {host['host']} in 24h (from {top})")

        if siris._unifi_client is not None:
            try:
                events = siris._unifi_client.ips_events(hours=24)
                report["intrusion_alerts"] = events[:20]
                if events:
                    kinds = sorted({str(e.get("category") or e.get("signature")) for e in events})[:3]
                    warnings.append(f"{len(events)} UniFi intrusion alerts in 24h ({', '.join(kinds)})")
            except Exception as exc:
                report["intrusion_alerts"] = {"unavailable": f"{type(exc).__name__}: {exc}"[:160]}

        scan = siris.state.get(TRIVY_KEY)
        if scan:
            report["image_vulnerabilities"] = scan
            critical = [i for i in scan["images"] if i.get("critical")]
            if critical:
                worst = ", ".join(f"{i['image']} ({i['critical']} critical)" for i in critical[:3])
                warnings.append(f"Container images with critical vulnerabilities: {worst}")

        backup = run_backup_check(siris, now)
        report["backups"] = backup
        urgent.extend(backup["warnings"])

        problems = urgent + warnings
        report["urgent"] = urgent
        report["warnings"] = warnings
        report["summary"] = (
            "Security check: all clear." if not problems else "Security check: " + "; ".join(problems) + "."
        )
        return report
