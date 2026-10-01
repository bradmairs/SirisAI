"""Short lines the daily briefing adds from personal integrations -- email,
health, car, parcels -- each only when that integration is configured, and
each best-effort (a dead mail server never breaks the briefing)."""

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.core.siris import SirisAI


def _email(siris: "SirisAI") -> str | None:
    if siris.tools.get("email_list_unread") is None:
        return None
    messages = siris.tools.get("email_list_unread").run(since_days=1)  # type: ignore[union-attr]
    important = [m for m in messages if m["important"]]
    if not messages:
        return "No unread email."
    line = f"Email: {len(important)} important of {len(messages)} unread"
    if important:
        line += " -- " + "; ".join(f"{m['from'].split('<')[0].strip() or m['from']}: {m['subject']}" for m in important[:3])
    return line + "."


def _health(siris: "SirisAI") -> str | None:
    result = siris.tools.get("health_summary").run()  # type: ignore[union-attr]
    metrics = result["metrics"]
    if not metrics:
        return None
    if result["notes"]:
        return "Health: " + "; ".join(result["notes"]) + "."
    bits = []
    if "sleep_hours" in metrics:
        bits.append(f"slept {metrics['sleep_hours']['latest']:.1f}h")
    if "steps" in metrics:
        bits.append(f"{metrics['steps']['latest']:,.0f} steps")
    return ("Health: " + ", ".join(bits) + ".") if bits else None


EXTRAS: list[Callable[["SirisAI"], str | None]] = [_email, _health]


def briefing_lines(siris: "SirisAI") -> list[str]:
    lines = []
    for extra in EXTRAS:
        try:
            line = extra(siris)
        except Exception:
            line = None
        if line:
            lines.append(line)
    return lines
