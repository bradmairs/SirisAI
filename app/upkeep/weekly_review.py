"""What the week's review in Siris adds beyond the notes (6.58).

The second brain's own weekly review (app/second_brain/jobs.py,
03-Resources/Weekly Reviews/<YYYY>-W<week>-review) is written from the
vault's insights. This adds what happened outside the vault, which it
passes to that review (and appends when there's no model):

- **Done**: goals completed, and what SirisAI did on its own, by area;
- **Notes and decisions**: what changed in Siris this week (its activity log);
- **Servers**: reboots, updates and problems, from each server's timeline;
- **Training**: the week's workouts, sets and records, and next week's
  change;
- **Sleep**: hours a night and readiness.

The weekly_review tool shows it any time.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from statistics import mean
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)


def _safe(fn: Any, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - one missing part never stops the review
        logger.debug("Part of the weekly review failed", exc_info=True)
        return default


def compose(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    from app.audit.autonomy import timeline as did
    from app.health import gym_week, overview
    from app.servers import timeline

    now = now or datetime.now(UTC)
    on = overview.today(siris) if now is None else now.astimezone(_zone(siris)).date()
    start = on - timedelta(days=6)
    year, week, _ = on.isocalendar()
    sections: dict[str, list[str]] = {}

    done = []
    goals = _safe(lambda: [g for g in siris.goals.list_goals(status="completed")
                           if str(g.get("updated_at") or g.get("completed_at") or "")[:10] >= start.isoformat()], [])
    done += [f"- Goal done: {g['title']}" for g in goals]
    acted = _safe(lambda: did(siris, hours=24 * 7, now=now), {})
    for group in (acted or {}).get("by_area") or []:
        done.append(f"- SirisAI, {group['name'].lower()}: {group['count']} action{'s' if group['count'] != 1 else ''}"
                    + (f" ({'; '.join(i['what'] for i in group['items'][:3])})" if group["items"] else ""))
    sections["Done"] = done

    notes = []
    if siris.second_brain is not None:
        for entry in (_safe(lambda: siris.second_brain.activity(7), []) or [])[:12]:
            line = str(entry.get("line") or "").strip()
            if line:
                notes.append(f"- {line}")
    sections["Notes and decisions"] = notes

    servers = []
    for name in _safe(lambda: timeline.servers(siris), []) or []:
        events = [e for e in timeline.events(siris, name, 7) if e["kind"] in ("power", "updates", "health", "disks")]
        if events:
            servers.append(f"- {name}: " + "; ".join(e["text"] for e in reversed(events[:6])))
    sections["Servers"] = servers

    training = _safe(lambda: gym_week.weekly_summary(siris, on + timedelta(days=(7 - on.weekday()) % 7 or 7)))
    sections["Training"] = [f"- {training['text']}"] if training else []

    sleep = []
    data = _safe(lambda: overview.days(siris, on, 8), {}) or {}
    hours = [data[d]["sleep_hours"] for d in data if d >= start.isoformat() and (data.get(d) or {}).get("sleep_hours")]
    if hours:
        sleep.append(f"- Slept {mean(hours):.1f} h a night ({len(hours)} nights, shortest {min(hours):.1f} h)")
    ready = [r["score"] for r in _safe(lambda: overview.recent_readiness(siris, on, 7), []) or [] if r.get("score") is not None]
    if ready:
        sleep.append(f"- Readiness averaged {mean(ready):.0f} (lowest {min(ready)})")
    sections["Sleep"] = sleep

    title = f"{year}-W{week:02d}-review"
    body = "\n\n".join(f"## {head}\n" + "\n".join(lines) for head, lines in sections.items() if lines)
    return {"title": title, "week": f"{year}-W{week:02d}", "from": start.isoformat(), "to": on.isoformat(), "sections": sections,
            "body": body, "content": f"Week {start:%-d %b} to {on:%-d %b %Y}.\n\n" + (body or "A quiet week: nothing recorded.")}


def _zone(siris: "SirisAI") -> Any:
    from app.core import clock

    return clock.get_zone(siris.settings.timezone)
