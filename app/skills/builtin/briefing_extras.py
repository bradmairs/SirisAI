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


def _car(siris: "SirisAI") -> str | None:
    if siris.tools.get("car_status") is None:
        return None
    status = siris.tools.get("car_status").run()  # type: ignore[union-attr]
    line = status["summary"] + "."
    if status.get("low_battery"):
        line += f" It's low and not plugged in -- charge it tonight."
    return line


def _parcels(siris: "SirisAI") -> str | None:
    active = siris.parcels.active()
    if not active:
        return None
    return "Parcels: " + "; ".join(
        f"{p['label']} {p['status_text']}" + (f" (ETA {p['eta'][:10]})" if p.get("eta") and p["status"] not in ("Delivered",) else "")
        for p in active[:5]
    ) + "."


def _sirisos_apps(siris: "SirisAI") -> str | None:
    """Apps SirisOS reports down or degraded -- it already watches them all,
    so SirisAI asks it rather than checking each one again."""
    tool = siris.tools.get("sirisos_apps_status")
    if tool is None:
        return None
    apps = [a for a in tool.run()["apps"] if a["state"] in ("down", "degraded")]
    if not apps:
        return None
    return "Apps needing a look: " + "; ".join(f"{a['name']} is {a['state']}" + (f" ({a['detail']})" if a.get("detail") else "") for a in apps[:5]) + "."


def _on_this_day(siris: "SirisAI") -> str | None:
    """One note from this day a year ago (or a month or two while the brain is young)."""
    if siris.second_brain is None:
        return None
    found = siris.second_brain.on_this_day(limit=1)
    if not found:
        return None
    f = found[0]
    text = f["text"] if len(f["text"]) <= 140 else f["text"][:140].rsplit(" ", 1)[0] + "…"
    return f"On this day, {f['when']}: {f['title']} -- {text}"


def _birthdays(siris: "SirisAI") -> str | None:
    """Birthdays in the next week, from People notes in the brain."""
    if siris.second_brain is None:
        return None
    soon = [b for b in siris.second_brain.people(days=7)["birthdays"]]
    if not soon:
        return None
    def when(b: dict) -> str:
        return "today" if b["days"] == 0 else "tomorrow" if b["days"] == 1 else f"in {b['days']} days"
    return "Birthdays: " + "; ".join(
        f"{b['title']} {when(b)}" + (f" (turning {b['turning']})" if b.get("turning") else "") for b in soon) + "."


def _training(siris: "SirisAI") -> str | None:
    """6.38: last week's training on Mondays; a lighter plan on a low-readiness training day."""
    from app.health import gym_week

    return gym_week.briefing_line(siris)


EXTRAS: list[Callable[["SirisAI"], str | None]] = [_email, _health, _training, _car, _parcels, _sirisos_apps, _on_this_day, _birthdays]


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
