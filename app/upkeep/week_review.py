"""What broke this week, and what fixed itself (4.29.0).

A few lines for the Sunday reliability report, so a slow-burning problem
(the self-test failing four nights running, 22-25 Nov 2026) shows up in one
place without reading the feed every day:

- the nightly self-test: how many nights were all right;
- what broke and came back (from the breakage watch), with how long;
- what's still broken;
- services SirisAI found at a new address, and code bugs real use turned up;
- the versions it ran this week.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app import __version__

if TYPE_CHECKING:
    from app.core.siris import SirisAI

LOG_KEY = "week_review:log"        # [{"at", "kind", "text"}], kept 35 days
VERSIONS_KEY = "week_review:versions"   # [{"version", "since"}]


def log(siris: "SirisAI", kind: str, text: str, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=35)).isoformat()
    items = [i for i in (siris.state.get(LOG_KEY) or []) if i["at"] >= cutoff]
    siris.state.set(LOG_KEY, [*items, {"at": now.isoformat(), "kind": kind, "text": text}][-300:])


def note_version(siris: "SirisAI", now: datetime | None = None) -> None:
    seen = list(siris.state.get(VERSIONS_KEY) or [])
    if not seen or seen[-1]["version"] != __version__:
        siris.state.set(VERSIONS_KEY, [*seen, {"version": __version__, "since": (now or datetime.now(UTC)).isoformat()}][-20:])


def _hours(a: str, b: str) -> str:
    span = datetime.fromisoformat(b) - datetime.fromisoformat(a)
    hours = span.total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def lines(siris: "SirisAI", now: datetime | None = None, days: int = 7) -> list[str]:
    from app.learning import tool_failures
    from app.upkeep import breakage, self_test

    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat()
    out: list[str] = []
    nights = [h for h in siris.state.get(self_test.HISTORY_KEY) or [] if h["at"] >= since]
    if nights:
        good = sum(1 for h in nights if not h["failed"])
        out.append(f"Self-test: {good} of {len(nights)} nights all right.")
    events = [i for i in siris.state.get(LOG_KEY) or [] if i["at"] >= since]
    wrong = [i["text"] for i in events if i["kind"] == "wrong"]  # 4.30.0: here, not a card each morning
    if wrong:
        out.append("Answered wrong: " + "; ".join(wrong[:6]) + ("." if len(wrong) <= 6 else f"; and {len(wrong) - 6} more."))
    unfixed = [i["text"] for i in events if i["kind"] == "unfixed"]
    if unfixed:
        out.append("Known fixes that didn't work: " + "; ".join(unfixed[:4]) + ".")
    fixed = [i["text"] for i in events if i["kind"] == "fixed"]
    if fixed:
        out.append("Broke and came back (fixed by itself or by SirisAI): " + "; ".join(fixed[:6]) + ".")
    still = [f"{v['title'].replace(' keeps failing', '')} (since {v['since'][:10]})" for v in (siris.state.get(breakage.STATE_KEY) or {}).values()]
    if still:
        out.append("Still broken: " + "; ".join(still[:6]) + ".")
    moved = [i["text"] for i in events if i["kind"] == "moved"]
    if moved:
        out.append("Found at a new address: " + "; ".join(moved[:4]) + ".")
    bugs = [f"{e['tool']} ({e['kind']}, {e['count']}x)" for e in tool_failures.recent(siris, 150) if e.get("code_bug") and e["last"] >= since]
    if bugs:
        out.append("Code bugs real use found: " + ", ".join(bugs[:5]) + ".")
    versions = [v["version"] for v in siris.state.get(VERSIONS_KEY) or [] if v["since"] >= since]
    if versions:
        out.append("Updated to " + " then ".join(versions) + ".")
    return out
