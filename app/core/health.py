"""Knowing when SirisAI's own tools and integrations are broken.

Found live (2026-09-26): Docker, Obsidian and the daily briefing failed over
and over -- 15 Docker failures -- while the model kept choosing those tools
and users kept getting "I couldn't". Two pieces:

- a circuit breaker: a tool whose recent calls all failed with an
  environmental error (connection refused, missing socket, timeout, 5xx) is
  left out of the tools offered to the model for a while, so it stops
  steering the model into a wall (`broken_tools`);
- reachability checks for every configured integration, on demand
  (`check_integrations`) and used to raise an alert when something is down.
"""

import re
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.siris import SirisAI

# Errors that mean "the thing behind this tool is unreachable", as opposed to
# the model passing a bad argument (which shouldn't hide a working tool).
_ENVIRONMENTAL_RE = re.compile(
    r"errno|connection|refused|timed out|timeout|no such file|name or service|unreachable|"
    r"bad gateway|service unavailable|gateway time-?out|server error '5\d\d|failed \(255\)|ssh .* failed",
    re.IGNORECASE,
)

BREAKER_WINDOW_MINUTES = 15
BREAKER_MIN_FAILURES = 3


def broken_tools(siris: "SirisAI", now: datetime | None = None) -> set[str]:
    """Tools with >= BREAKER_MIN_FAILURES environmental failures and no
    success in the last BREAKER_WINDOW_MINUTES."""
    cutoff = (now or datetime.now(UTC)).replace(tzinfo=None) - timedelta(minutes=BREAKER_WINDOW_MINUTES)
    stats = siris.audit.recent_outcomes(since=cutoff)
    broken = set()
    for name, outcome in stats.items():
        if outcome["successes"] == 0 and outcome["environmental_failures"] >= BREAKER_MIN_FAILURES:
            broken.add(name)
    return broken


def is_environmental_error(error: str | None) -> bool:
    return bool(error) and _ENVIRONMENTAL_RE.search(error or "") is not None


def _integration_targets(settings: "Settings") -> list[tuple[str, str]]:
    candidates = [
        ("Ollama", settings.ollama_base_url if settings.llm_provider == "ollama" else ""),
        ("Home Assistant", settings.home_assistant_base_url),
        ("Plex", settings.plex_base_url),
        ("Tautulli", settings.tautulli_base_url),
        ("Radarr", settings.radarr_base_url),
        ("Sonarr", settings.sonarr_base_url),
        ("Bazarr", settings.bazarr_base_url),
        ("UniFi", settings.unifi_base_url),
        ("Obsidian", settings.obsidian_base_url if settings.obsidian_api_key else ""),
        ("Hermes", settings.hermes_base_url),
        ("Frigate", settings.frigate_base_url),
        ("Synology", settings.synology_base_url),
        ("Listenarr", settings.listenarr_base_url),
        ("Cleanuparr", settings.cleanuparr_base_url),
        ("SirisOS", f"{settings.sirisos_url.rstrip('/')}/health" if settings.sirisos_url else ""),
    ]
    return [(name, url) for name, url in candidates if url]


def check_integrations(
    settings: "Settings",
    cron_hosts: list[Any] | None = None,
    timeout: float = 4.0,
    second_brain: Any = None,
) -> list[dict[str, Any]]:
    """Reachability of every configured integration: any HTTP response (even
    401/404) means the service is up; a connection error means it's down."""
    results: list[dict[str, Any]] = []
    for name, url in _integration_targets(settings):
        started = time.perf_counter()
        try:
            httpx.get(url, timeout=timeout, verify=False, follow_redirects=False)
            results.append({"name": name, "ok": True, "detail": "reachable", "ms": round((time.perf_counter() - started) * 1000)})
        except Exception as exc:  # noqa: BLE001 - any failure to connect is the finding
            results.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:160], "ms": None})

    docker_url = settings.docker_base_url
    if docker_url.startswith("unix://"):
        import os

        ok = os.path.exists(docker_url.removeprefix("unix://"))
        results.append({"name": "Docker", "ok": ok, "detail": "socket present" if ok else "socket not mounted in this container", "ms": None})

    if settings.brain_path:
        from pathlib import Path

        ok = (Path(settings.brain_path) / "tools" / "brain.py").exists()
        detail = "vault present" if ok else f"no tools/brain.py under {settings.brain_path}"
        sync_error = getattr(second_brain, "last_sync_error", None)
        if ok and sync_error:
            ok, detail = False, sync_error
        results.append({"name": "Second Brain", "ok": ok, "detail": detail, "ms": None})

    for host in cron_hosts or []:
        started = time.perf_counter()
        try:
            with socket.create_connection((host.host, host.port), timeout=timeout):
                pass
            results.append({"name": f"SSH {host.label}", "ok": True, "detail": "port open", "ms": round((time.perf_counter() - started) * 1000)})
        except OSError as exc:
            results.append({"name": f"SSH {host.label}", "ok": False, "detail": str(exc)[:120], "ms": None})
    return results
