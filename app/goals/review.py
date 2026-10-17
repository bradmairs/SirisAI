"""The weekly goal review: goals that take months (chartership, CPD, a
renovation) get looked at every week without being asked.

For each active goal it raises one item in the attention feed with where
it stands ("3 of 7 done"), the next open step, and how long since anything
moved. "Done that" ticks off the next step; "Not this week" puts it away
until next week. A goal with no steps yet is asked to be broken down. The
same item is updated each week rather than piling up.

Runs on SIRISAI_GOAL_REVIEW_DAY (0 = Monday, default 0) at
SIRISAI_GOAL_REVIEW_HOUR_LOCAL (default 8); unset the hour to turn it off.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

SOURCE = "goals"


def _days_since(iso: str, now: datetime) -> int:
    value = datetime.fromisoformat(iso)
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return max(0, (now - value).days)


def review(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    week = now.strftime("%G-W%V")
    reviewed = []
    goals = siris.goals.list_goals("active")
    for goal in goals:
        steps = goal["objectives"]
        done = sum(1 for o in steps if o["done"])
        upcoming = next((o for o in steps if not o["done"]), None)
        idle = _days_since(goal["updated_at"], now)
        moved = "moved this week" if idle < 7 else f"last moved {idle} days ago"
        if not steps:
            title = f"{goal['title']}: no steps yet"
            body = f"This goal has no steps, so there's nothing to check off. Want to break it down? Ask me to add the first few ({moved})."
            actions = [action("acknowledge", "Got it", "primary")]
        elif upcoming is None:
            title = f"{goal['title']}: every step done"
            body = "All its steps are done. Mark the goal complete, or add what comes next."
            actions = [action("acknowledge", "Got it", "primary")]
        else:
            title = f"{goal['title']}: {done} of {len(steps)} done"
            body = f"Next: {upcoming['description']}. It {moved}."
            actions = [action("complete", "Done that", "primary"), action("not_now", "Not this week")]
        siris.attention.raise_item(
            f"goals:review:{goal['id']}",
            "review",
            title,
            source=SOURCE,
            severity="info" if idle < 14 else "attention",
            body=body,
            actions=actions,
            payload={"goal_id": goal["id"], "objective_id": upcoming["id"] if upcoming else None, "week": week},
        )
        reviewed.append({"goal": goal["title"], "done": done, "steps": len(steps), "next": upcoming["description"] if upcoming else None, "idle_days": idle})
    active_ids = {f"goals:review:{g['id']}" for g in goals}
    for item in siris.attention.list("open", limit=500):
        if item["source"] == SOURCE and item["key"] not in active_ids:
            siris.attention.close(item["id"], "done", "Goal no longer active")
    return reviewed


def _complete(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = siris.run_tool("complete_goal_objective", {"objective_id": int(payload["objective_id"])}, trigger=trigger)
    return result, "Step done"


def _not_now(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    return None, "Put off until next week"


register_action(SOURCE, "complete", _complete)
register_action(SOURCE, "not_now", _not_now)
