"""Plain words for what went wrong (6.73), for the weekly reports in the feed.

Brad reads these on his phone: "Home Assistant had an error on its side 3
times" means something to him, a 500 with a URL and a link to MDN doesn't.
The raw errors stay in the API (GET /siris/reliability, /siris/audit) and
the web UI's Activity tab for whoever is fixing it.
"""

from __future__ import annotations

import re
from collections import Counter

# Tool name (or its prefix) -> the service Brad knows it as.
_SERVICES: list[tuple[str, str]] = [
    ("notify", "Push notifications"),
    ("unifi_", "UniFi"),
    ("presence", "The who's-home check"),
    ("departure", "The departure check"),
    ("home_assistant_", "Home Assistant"),
    ("docker_", "Docker"),
    ("frigate_", "The cameras"),
    ("gmail_", "Email"),
    ("email_", "Email"),
    ("calendar_", "The calendar"),
    ("plex_", "Plex"),
    ("ssh_", "The servers"),
    ("server_", "The servers"),
    ("speed_test", "The speed test"),
    ("run_speed_test", "The speed test"),
    ("weather", "The weather"),
    ("second_brain", "Siris"),
    ("brain_", "Siris"),
]

# (pattern, what happened) in order: the first match wins.
_REASONS: list[tuple[str, str]] = [
    (r"413|too large", "a message was too long to send"),
    (r"timed? ?out|timeout", "it took too long to answer"),
    (r"errno 111|connection refused|connecterror|name or service not known|unreachable|no route to host|errno -?\d+",
     "it couldn't be reached"),
    (r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid (api )?key", "it turned down SirisAI's login or key"),
    (r"\b404\b|not found", "it was asked for something that isn't there"),
    (r"\b5\d\d\b|server error|bad gateway|service unavailable", "it had a problem on its side"),
    (r"\b400\b|bad request|invalid", "it didn't accept what SirisAI sent"),
]

_FIXES = {
    ("Push notifications", "a message was too long to send"): "fixed in SirisAI 6.73: pushes are now shortened to fit",
    ("Home Assistant", "it was asked for something that isn't there"): "a device or sensor was renamed or removed in Home Assistant",
    ("Home Assistant", "it had a problem on its side"): "check Home Assistant's own log (Settings, System, Logs)",
    ("Home Assistant", "it couldn't be reached"): "check Home Assistant is running",
    ("UniFi", "it took too long to answer"): "the UniFi console was slow; restart it if this keeps happening",
    ("UniFi", "it couldn't be reached"): "check the UniFi console is on",
    ("UniFi", "it had a problem on its side"): "the UniFi console was busy or restarting; restart it if this keeps happening",
    ("The who's-home check", "it couldn't be reached"): "it asks UniFi, so check the UniFi console is on",
    ("The who's-home check", "it took too long to answer"): "it asks UniFi, which was slow",
}


def service(tool: str) -> str:
    for prefix, name in _SERVICES:
        if tool == prefix or tool.startswith(prefix):
            return name
    return tool.replace("_", " ").strip().capitalize() or "Something"


def reason(error: str | None) -> str:
    text = (error or "").lower()
    for pattern, said in _REASONS:
        if re.search(pattern, text):
            return said
    return "something went wrong"


def fix(name: str, why: str) -> str | None:
    return _FIXES.get((name, why))


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def summarise(failures: list[tuple[str, str | None]], limit: int = 5) -> list[str]:
    """[(tool, error)] -> one plain line per service, busiest first:
    "Home Assistant failed 5 times, mostly because it had a problem on its
    side. Fix: check Home Assistant's own log."""
    by_service: dict[str, Counter] = {}
    for tool, error in failures:
        by_service.setdefault(service(tool), Counter())[reason(error)] += 1
    lines = []
    for name, reasons in sorted(by_service.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(reasons.values())
        why, _ = reasons.most_common(1)[0]
        line = f"{name} failed {_times(total)}, {'mostly ' if len(reasons) > 1 else ''}because {why}."
        advice = fix(name, why)
        if advice:
            line += f" {advice[0].upper()}{advice[1:]}."
        lines.append(line)
    return lines[:limit]
