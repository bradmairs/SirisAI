""""That was wrong" (7.17): a thumbs-down on SirisAI's last answer.

The tap is recorded as a `corrected` outcome for whatever the turn did, and
the request becomes a tool-choice case the nightly evals keep checking
(app/evals/tool_choice.py):

- with `right_tool` ("it should have used ..."): that tool is expected;
- otherwise, when a tool ran, that tool is to be avoided for the request;
- otherwise (a plain reply that was wrong) it's kept for review only.

Every tap is listed at GET /siris/learning/feedback, newest first.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import Message
from app.learning.reflection import LEARNED_CASES_KEY, MAX_LEARNED_CASES, learned_cases

if TYPE_CHECKING:
    from app.core.siris import SirisAI

FEEDBACK_KEY = "reply_feedback"
MAX_FEEDBACK = 200


def last_turn(siris: "SirisAI", conversation_id: str) -> dict[str, Any] | None:
    """The last request in a conversation, the reply to it and the tools it ran."""
    with siris.conversation._session_factory() as session:
        rows = session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
        ).all()
        messages = [(m.id, m.role, m.content or "", m.tool_name) for m in rows]
    starts = [i for i, m in enumerate(messages) if m[1] == "user"]
    if not starts:
        return None
    start = starts[-1]
    after = messages[start + 1 :]
    replies = [m[2] for m in after if m[1] == "assistant" and m[2].strip()]
    return {
        "message_id": messages[start][0],
        "request": messages[start][2],
        "reply": replies[-1] if replies else "",
        "tools": [m[3] for m in after if m[1] == "tool" and m[3]],
    }


def record(siris: "SirisAI", conversation_id: str, note: str = "", right_tool: str | None = None) -> dict[str, Any]:
    turn = last_turn(siris, conversation_id)
    if turn is None:
        raise LookupError("No request in that conversation yet.")
    right = (right_tool or "").strip() or None
    if right is not None and siris.tools.get(right) is None:
        raise ValueError(f"There's no tool called '{right}'.")
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "conversation_id": conversation_id,
        "request": turn["request"][:500],
        "reply": turn["reply"][:1000],
        "tools": turn["tools"],
        "note": note.strip()[:500],
        "right_tool": right,
        "case": None,
    }
    subject = turn["tools"][0] if turn["tools"] else "chat"
    siris.outcomes.record(subject, "corrected", source="thumbs", detail=entry, ref=f"thumbs:{turn['message_id']}")

    case: dict[str, Any] | None = None
    request = turn["request"].strip()
    if request and right:
        case = {"prompt": request[:500], "expect": [right]}
    elif request and turn["tools"] and turn["tools"][0] not in ("remember", "recall"):
        case = {"prompt": request[:500], "avoid": [turn["tools"][0]]}
    if case is not None:
        case["learned"] = entry["at"][:10]
        case["from"] = "thumbs"
        cases = [c for c in learned_cases(siris) if c["prompt"].strip().lower() != request.lower()]
        siris.state.set(LEARNED_CASES_KEY, [*cases, case][-MAX_LEARNED_CASES:])
        entry["case"] = case

    kept = siris.state.get(FEEDBACK_KEY) or []
    siris.state.set(FEEDBACK_KEY, [*kept, entry][-MAX_FEEDBACK:])
    return entry


def recent(siris: "SirisAI", limit: int = 50) -> list[dict[str, Any]]:
    return list(reversed(siris.state.get(FEEDBACK_KEY) or []))[:limit]
