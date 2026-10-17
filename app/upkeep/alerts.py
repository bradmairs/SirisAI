"""One way for the upkeep jobs to say something: an item in the attention
feed (source "upkeep"), a push for urgent ones, and -- for problems SirisAI
can't fix itself -- a proposed GitHub issue (app/upkeep/issues.py)."""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
SOURCE = "upkeep"


def raise_alert(
    siris: "SirisAI",
    key: str,
    title: str,
    body: str,
    *,
    severity: str = "attention",
    push: bool = False,
    issue: bool = False,
) -> None:
    try:
        kind = "alert" if severity != "info" else "notice"
        siris.attention.raise_item(key, kind, title, source=SOURCE, severity=severity, body=body)
    except Exception:
        logger.exception("Raising upkeep alert %s failed", key)
    # Without a push channel the feed item above is the whole message (queuing
    # it for the briefing too showed it twice in the feed).
    if push and siris.tools.get("notify") is not None:
        try:
            siris.run_tool(
                "notify",
                {"title": f"SirisAI: {title}", "message": body, "priority": "urgent" if severity == "urgent" else "high"},
                trigger=f"upkeep: {key}",
            )
        except Exception:
            logger.exception("Pushing upkeep alert %s failed", key)
    if issue:
        try:
            from app.upkeep import issues

            issues.propose(siris, title, body, fingerprint=key)
        except Exception:
            logger.exception("Proposing an issue for %s failed", key)
