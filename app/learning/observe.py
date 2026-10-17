"""Turning what already happened into outcomes (app/learning/outcomes.py).

Everything here reads records SirisAI already keeps -- the audit log, the
chat history, the attention feed -- so it can run any time and catch up.
Each outcome carries a `ref` naming what it came from, so running twice
never counts anything twice.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.audit.autonomy import is_autonomous
from app.db.models import Message
from app.learning.feedback import action_key

if TYPE_CHECKING:
    from app.core.siris import SirisAI

UNDO_WINDOW = timedelta(minutes=15)
PROTOCOL_UNDO_WINDOW = timedelta(minutes=60)
IGNORED_AFTER = timedelta(days=3)

_OPPOSITE = {
    "turn_on": "turn_off",
    "turn_off": "turn_on",
    "open_cover": "close_cover",
    "close_cover": "open_cover",
    "lock": "unlock",
    "unlock": "lock",
    "toggle": "toggle",
    "media_play": "media_pause",
    "media_pause": "media_play",
}

# "no, the other one", "I meant the kitchen", "that's wrong", "not that".
CORRECTION_RE = re.compile(
    r"^\s*(no\b|nope\b|not that\b|not what i\b|wrong\b|that'?s (not|wrong)\b|that was(n'?t| not| wrong)\b|"
    r"i (meant|said|asked)\b|undo\b|actually,? (i|the|turn|can)\b)",
    re.IGNORECASE,
)


def _parse(iso: str) -> datetime:
    value = datetime.fromisoformat(iso)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _entities(arguments: dict[str, Any]) -> set[str]:
    entity = arguments.get("entity_id")
    if isinstance(entity, list):
        return {str(e) for e in entity}
    return {str(entity)} if entity else set()


def undos(siris: "SirisAI", now: datetime | None = None, lookback: timedelta = timedelta(days=2)) -> list[dict[str, Any]]:
    """Judge each autonomous device change and protocol run old enough to
    have been undone: `undone` if the user reversed it within the window,
    `kept` otherwise. Returns the outcomes recorded this time."""
    now = now or datetime.now(UTC)
    entries = siris.audit.since(now - lookback, limit=5000)
    recorded = []
    for i, entry in enumerate(entries):
        if entry["error"] or entry["confirmation_required"] or not is_autonomous(entry.get("trigger")):
            continue
        tool, args = entry["tool_name"], entry["arguments"] or {}
        at = _parse(entry["created_at"])
        if tool == "home_assistant_call_service":
            opposite = _OPPOSITE.get(str(args.get("service")))
            entities = _entities(args)
            if opposite is None or not entities:
                continue
            window = UNDO_WINDOW
            undo = next(
                (
                    later
                    for later in entries[i + 1 :]
                    if _parse(later["created_at"]) - at <= window
                    and later["tool_name"] == tool
                    and not later["error"]
                    and not is_autonomous(later.get("trigger"))
                    and str((later["arguments"] or {}).get("service")) == opposite
                    and _entities(later["arguments"] or {}) & entities
                ),
                None,
            )
        elif tool == "run_protocol":
            window = PROTOCOL_UNDO_WINDOW
            undo = next(
                (
                    later
                    for later in entries[i + 1 :]
                    if _parse(later["created_at"]) - at <= window
                    and later["tool_name"] == "undo_last_protocol"
                    and not later["error"]
                    and not later["confirmation_required"]
                ),
                None,
            )
        else:
            continue
        if undo is None and now - at < window:
            continue  # too soon to say it was kept
        signal = "undone" if undo is not None else "kept"
        detail = {"trigger": entry.get("trigger"), "arguments": args, "at": entry["created_at"]}
        if undo is not None:
            detail["undone_at"] = undo["created_at"]
        if siris.outcomes.record(tool, signal, key=action_key(tool, args), source="audit", detail=detail, ref=f"audit:{entry['id']}"):
            recorded.append({"tool": tool, "key": action_key(tool, args), "signal": signal})
    return recorded


def unverified(siris: "SirisAI", now: datetime | None = None, lookback: timedelta = timedelta(days=2)) -> list[dict[str, Any]]:
    """Tool calls whose own check afterwards said they didn't work."""
    now = now or datetime.now(UTC)
    recorded = []
    for entry in siris.audit.since(now - lookback, limit=5000):
        if entry.get("verified") is not False:
            continue
        tool, args = entry["tool_name"], entry["arguments"] or {}
        detail = {"why": entry.get("verification_detail"), "trigger": entry.get("trigger"), "arguments": args}
        if siris.outcomes.record(tool, "unverified", key=action_key(tool, args), source="audit", detail=detail, ref=f"verify:{entry['id']}"):
            recorded.append({"tool": tool, "why": entry.get("verification_detail")})
    return recorded


def corrections(siris: "SirisAI", now: datetime | None = None, lookback: timedelta = timedelta(days=2)) -> list[dict[str, Any]]:
    """Chat turns where a tool ran and the user's next message corrected it
    ("no, I meant the kitchen"). Each is a `corrected` outcome for the tool
    that ran; when a different tool ran after the correction, it's also an
    eval case: that request should have gone to that tool."""
    now = now or datetime.now(UTC)
    cutoff = (now - lookback).astimezone(UTC).replace(tzinfo=None)
    session_factory = siris.conversation._session_factory
    with session_factory() as session:
        ids = session.scalars(select(Message.conversation_id).where(Message.created_at >= cutoff).distinct()).all()
        conversations = {
            cid: session.scalars(select(Message).where(Message.conversation_id == cid).order_by(Message.created_at, Message.id)).all()
            for cid in ids
        }
        turns = {
            cid: [(m.id, m.role, m.content or "", m.tool_name) for m in messages] for cid, messages in conversations.items()
        }
    found = []
    for cid, messages in turns.items():
        user_turns = [i for i, m in enumerate(messages) if m[1] == "user"]
        for n, start in enumerate(user_turns[:-1]):
            correction_at = user_turns[n + 1]
            msg_id, _, text, _ = messages[correction_at]
            if not CORRECTION_RE.match(text):
                continue
            wrong = [m[3] for m in messages[start + 1 : correction_at] if m[1] == "tool" and m[3]]
            if not wrong:
                continue
            end = user_turns[n + 2] if n + 2 < len(user_turns) else len(messages)
            right = [m[3] for m in messages[correction_at + 1 : end] if m[1] == "tool" and m[3]]
            case = {
                "conversation_id": cid,
                "request": messages[start][2],
                "correction": text,
                "wrong_tool": wrong[0],
                "right_tool": right[0] if right else None,
            }
            if siris.outcomes.record(wrong[0], "corrected", source="chat", detail=case, ref=f"msg:{msg_id}"):
                found.append(case)
    return found


def ignored_items(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    """Suggestions and approvals left open in the attention feed for days."""
    now = now or datetime.now(UTC)
    recorded = []
    for item in siris.attention.list("open", limit=500):
        if item["kind"] not in ("suggestion", "confirm") or now - _parse(item["created_at"]) < IGNORED_AFTER:
            continue
        if siris.outcomes.record(
            f"attention:{item['source']}", "ignored", key=item["key"], source="attention", detail={"title": item["title"]}, ref=f"attention-ignored:{item['id']}"
        ):
            recorded.append({"title": item["title"], "source": item["source"]})
    return recorded


_ACCEPT = {"approve", "accept", "remember", "lockdown", "complete", "apply", "file", "send", "save"}
_DISMISS = {"decline", "dismiss", "not_now", "skip"}


def on_attention_action(siris: "SirisAI", item: dict[str, Any], action_id: str, payload: dict[str, Any]) -> None:
    """The user answered something in the feed. Recorded for the item's
    source, and -- for an approval of something SirisAI wanted to do on its
    own -- for that specific action, so demotion can lift again."""
    store = getattr(siris, "outcomes", None)
    if store is None:
        return
    signal = "accepted" if action_id in _ACCEPT else "dismissed" if action_id in _DISMISS else None
    if signal is None or item["source"] == "habits":  # habits.set_status() records those, however they're answered
        return
    store.record(
        f"attention:{item['source']}", signal, key=item["key"], source="attention", detail={"title": item["title"], "action": action_id}, ref=f"attention:{item['id']}"
    )
    if item["kind"] == "confirm" and payload.get("tool") and is_autonomous(payload.get("context")):
        tool, args = payload["tool"], payload.get("arguments") or {}
        store.record(tool, signal, key=action_key(tool, args), source="attention", detail={"action": action_id}, ref=f"attention-action:{item['id']}")
