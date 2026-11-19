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
