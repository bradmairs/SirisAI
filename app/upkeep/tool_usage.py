"""Which tools are used, and which never are (7.17).

SirisAI has ~250 tools. Every one the model is offered costs prompt tokens
and gives a small model another way to go wrong, so this lists, from the
audit log, how often each registered tool ran over the last `days` (capped
at SIRISAI_AUDIT_KEEP_DAYS, since older entries are pruned), and the
families where nothing ran at all: candidates to merge or remove. Nothing
is removed automatically; see GET /siris/upkeep/tool-usage.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.db.models import ToolExecution
from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI


def _family(name: str) -> str:
    return name.split("_", 1)[0]


def report(siris: "SirisAI", days: int = 60, now: datetime | None = None) -> dict[str, Any]:
    keep = getattr(siris.settings, "audit_keep_days", 0) or 0
    days = max(1, min(days, keep)) if keep > 0 else max(1, days)
    since = ((now or datetime.now(UTC)) - timedelta(days=days)).astimezone(UTC).replace(tzinfo=None)
    with siris.audit._session_factory() as session:
        rows = session.execute(
            select(ToolExecution.tool_name, func.count(ToolExecution.id)).where(ToolExecution.created_at >= since).group_by(ToolExecution.tool_name)
        ).all()
    runs = Counter({name: int(count) for name, count in rows})
    registered = sorted(t.name for t in siris.tools.list())
    unused = [n for n in registered if not runs.get(n)]
    families: dict[str, list[str]] = {}
    for name in registered:
        families.setdefault(_family(name), []).append(name)
    idle_families = sorted(f for f, names in families.items() if all(not runs.get(n) for n in names) and len(names) > 1)
    return {
        "days": days,
        "registered": len(registered),
        "used": len(registered) - len(unused),
        "unused": unused,
        "idle_families": {f: families[f] for f in idle_families},
        "most_used": [{"tool": n, "runs": c} for n, c in runs.most_common(15) if n in set(registered)],
    }


FEED_KEY = "upkeep:tool_trim"
SOURCE = "tool_trim"


def audit_days(siris: "SirisAI", now: datetime | None = None) -> float:
    """How many days the audit log covers (it starts empty on a new install)."""
    with siris.audit._session_factory() as session:
        first = session.scalar(select(func.min(ToolExecution.created_at)))
    if first is None:
        return 0.0
    return ((now or datetime.now(UTC)).replace(tzinfo=None) - first).total_seconds() / 86400


def candidates(siris: "SirisAI", now: datetime | None = None) -> list[str]:
    """Unused tools worth hiding (7.27): never ones that ask first or are restricted
    (rare but important), core ones, or ones already hidden."""
    from app.plugins.base import ToolPermission
    from app.plugins.selector import CORE_TOOLS

    days = siris.settings.tool_trim_after_days
    if days <= 0 or audit_days(siris, now) < days:
        return []
    keep = {
        t.name for t in siris.tools.list()
        if t.permission in (ToolPermission.CONFIRM_REQUIRED, ToolPermission.RESTRICTED)
    } | set(CORE_TOOLS) | siris.hidden_tool_names()
    return [n for n in report(siris, days, now)["unused"] if n not in keep]


def propose(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    """Monthly: put the unused tools in the feed, to hide with one tap."""
    names = candidates(siris, now)
    if not names:
        return None
    days = siris.settings.tool_trim_after_days
    shown = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
    return siris.attention.raise_item(
        FEED_KEY,
        "suggestion",
        f"{len(names)} tools you haven't used in {days} days",
        source=SOURCE,
        severity="info",
        body=(
            f"Hiding them means the model has fewer to choose from, so answers are faster and it picks the right one "
            f"more often. They still work from routines and the feed, and you can bring any back on the Settings page "
            f"(Tools the model isn't offered). {shown}."
        ),
        actions=[action("hide", "Hide them", "primary"), action("dismiss", "Keep them")],
        payload={"tools": names},
    )


def _hide(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    from app.core import settings_overrides

    names = payload.get("tools") or []
    hidden = sorted(siris.hidden_tool_names() | set(names))
    result = settings_overrides.update(siris, {"hidden_tools": ", ".join(hidden)})
    return result, f"Hid {len(names)} tools"


register_action(SOURCE, "hide", _hide)
