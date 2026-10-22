"""The attention feed: one list of everything that needs the user, for
SirisOS's inbox and anything else that wants it (GET /siris/hub/v1/attention,
live at .../attention/stream).

Two kinds of producer:

- **Raised** as they happen: every push notification (`notify`, whoever sent
  it -- the intruder check, self-healing, the security watch...) becomes an
  alert, and an unattended action that needs confirmation (a routine, a chat
  turn nobody is watching) becomes an approval with Approve/Decline. A push
  with the same title and message as an open alert updates it instead of
  adding another.
- **Synced** from state SirisAI already keeps: open habit suggestions, jobs
  waiting for cheap power, non-urgent notifications queued for the briefing,
  and second-brain links waiting for review. A sync raises what's new,
  leaves alone what the user dismissed, and closes items whose underlying
  thing went away (accepted in chat, run, delivered in the briefing).

Acting on an item does the real thing (approving runs the held tool call
with confirmed=True, accepting a habit creates its automation, cancelling a
job cancels it) under a trigger naming the feed, so the audit log and the
autonomy timeline say where the decision came from. Dismissing hides it
everywhere -- the dismissal is server-side, so the phone and the desktop
agree.

Subscribers (the SSE stream) get every change pushed to them from whatever
thread made it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import AttentionItem

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

SEVERITIES = ("urgent", "attention", "info")
BRAIN_SYNC_SECONDS = 600


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat()


def action(id: str, label: str, style: str = "secondary", confirm: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": id, "label": label, "style": style}
    if confirm:
        out["confirm"] = confirm
    return out


ACKNOWLEDGE = action("acknowledge", "Got it", "primary")


class AttentionFeed:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()
        self._last_brain_sync: float | None = None  # None = never (monotonic() can be small just after boot)

    # -- reading -----------------------------------------------------------

    def list(self, status: str | None = "open", limit: int = 100) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = select(AttentionItem).order_by(AttentionItem.updated_at.desc()).limit(limit)
            if status:
                stmt = stmt.where(AttentionItem.status == status)
            rows = session.scalars(stmt).all()
            items = [self._serialize(r) for r in rows]
        if status == "open":
            items.sort(key=lambda i: (SEVERITIES.index(i["severity"]), -_ts(i["updated_at"])))
        return items

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.get(AttentionItem, item_id)
            return self._serialize(row) if row else None

    def counts(self) -> dict[str, int]:
        open_items = self.list("open", limit=500)
        return {"open": len(open_items), "urgent": sum(1 for i in open_items if i["severity"] == "urgent")}

    def _payload(self, item_id: int) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.get(AttentionItem, item_id)
            return json.loads(row.payload or "{}") if row else {}

    # -- writing -----------------------------------------------------------

    def raise_item(
        self,
        key: str,
        kind: str,
        title: str,
        *,
        source: str,
        severity: str = "attention",
        body: str = "",
        link: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        payload: dict[str, Any] | None = None,
        reopen: bool = True,
    ) -> dict[str, Any] | None:
        """Open an item, or update the open one with the same key. With
        reopen=False a key the user already dismissed or finished stays
        closed (synced sources: don't re-suggest what was declined)."""
        with self._session_factory() as session:
            rows = session.scalars(select(AttentionItem).where(AttentionItem.key == key).order_by(AttentionItem.id.desc())).all()
            current = next((r for r in rows if r.status == "open"), None)
            if current is None and rows and not reopen:
                return None
            row = current or AttentionItem(key=key, created_at=_now())
            row.kind, row.severity, row.title, row.body = kind, severity, title[:300], body[:2000]
            row.source, row.link = source, link
            row.actions = json.dumps(actions if actions is not None else [ACKNOWLEDGE])
            row.payload = json.dumps(payload or {}, default=str)
            row.status, row.updated_at = "open", _now()
            if current is None:
                session.add(row)
            session.commit()
            item = self._serialize(row)
        self._publish("upsert", item)
        return item

    def close(self, item_id: int, status: str, outcome: str | None = None) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.get(AttentionItem, item_id)
            if row is None:
                return None
            row.status, row.outcome = status, (outcome or "")[:500] or None
            row.updated_at = row.resolved_at = _now()
            session.commit()
            item = self._serialize(row)
        self._publish("remove", item)
        return item

    def resolve_key(self, key: str, outcome: str) -> None:
        with self._session_factory() as session:
            ids = [r.id for r in session.scalars(select(AttentionItem).where(AttentionItem.key == key, AttentionItem.status == "open"))]
        for item_id in ids:
            self.close(item_id, "done", outcome)

    def sync(self, source: str, wanted: list[dict[str, Any]], outcome: str = "No longer needed") -> None:
        """Make the open items from `source` match `wanted` (raise_item kwargs,
        each with a `key`): new ones open, dismissed ones stay dismissed,
        open ones that aren't wanted any more close as done."""
        keys = set()
        for spec in wanted:
            spec = dict(spec)
            key = spec.pop("key")
            keys.add(key)
            self.raise_item(key, spec.pop("kind"), spec.pop("title"), source=source, reopen=False, **spec)
        with self._session_factory() as session:
            stale = [
                r.id
                for r in session.scalars(select(AttentionItem).where(AttentionItem.source == source, AttentionItem.status == "open"))
                if r.key not in keys
            ]
        for item_id in stale:
            self.close(item_id, "done", outcome)

    # -- live updates --------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]

    def _publish(self, kind: str, item: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        if not subscribers:
            return
        event = {"type": kind, "items": [item], "counts": self.counts()}
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, event)
            except RuntimeError:  # that loop has closed; its stream is gone
                self.unsubscribe(queue)

    @staticmethod
    def _serialize(row: AttentionItem) -> dict[str, Any]:
        return {
            "id": row.id,
            "key": row.key,
            "kind": row.kind,
            "severity": row.severity,
            "title": row.title,
            "body": row.body or "",
            "source": row.source,
            "link": row.link,
            "actions": json.loads(row.actions or "[]"),
            "status": row.status,
            "outcome": row.outcome,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }


def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:  # a stalled client misses updates, not the server
        pass


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _digest(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


# -- producers ----------------------------------------------------------------


def source_from_trigger(trigger: str | None) -> str:
    """'self-healing: watched container plex is down' -> 'self-healing'."""
    if not trigger:
        return "siris"
    head = trigger.split(":", 1)[0].strip()
    return head[:60] or "siris"


def on_push(siris: "SirisAI", arguments: dict[str, Any], trigger: str | None) -> None:
    """Every push notification also lands in the feed -- except a
    confirmation request (notify_confirmation_needed), which
    raise_confirmation() already put there with working Approve/Decline."""
    if getattr(siris, "attention", None) is None:  # a stand-in SirisAI without a feed
        return
    if arguments.get("actions") or (trigger or "").endswith(": action needed confirmation"):
        return
    if (trigger or "").startswith("upkeep:"):  # app/upkeep/alerts.py raised its own item before pushing
        return
    title = str(arguments.get("title") or "SirisAI")
    message = str(arguments.get("message") or "")
    priority = arguments.get("priority")
    source = source_from_trigger(trigger)
    severity = "urgent" if priority in ("urgent", "high") or source in ("intruder_check", "self-healing") else "attention"
    link = None
    if source == "intruder_check":
        link = "cameras"
    actions = [ACKNOWLEDGE]
    if source == "intruder_check":
        actions = [
            action("lockdown", "Lock down", "danger", confirm="Run the lockdown protocol now?"),
            action("acknowledge", "It's fine", "secondary"),
        ]
    siris.attention.raise_item(
        f"push:{_digest(title, message)}", "alert", title, source=source, severity=severity, body=message, link=link, actions=actions
    )


def raise_confirmation(siris: "SirisAI", context: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if getattr(siris, "attention", None) is None:  # a stand-in SirisAI without a feed
        return None
    description = tool_name
    try:
        from app.voice.confirmations import describe_call

        description = describe_call(tool_name, arguments)
    except Exception:  # noqa: BLE001 - a plain tool name is a fine fallback
        pass
    return siris.attention.raise_item(
        f"confirm:{_digest(context, tool_name, json.dumps(arguments, sort_keys=True, default=str))}",
        "confirm",
        f"Approve: {description}",
        source=source_from_trigger(context),
        severity="attention",
        body=f"{context} wants to run {tool_name} and needs your OK.",
        actions=[action("approve", "Approve", "primary"), action("decline", "Decline", "secondary")],
        payload={"tool": tool_name, "arguments": arguments, "context": context},
    )


def sync_all(siris: "SirisAI", *, include_brain: bool = True) -> None:
    """Refresh every synced source. Each is best-effort on its own."""
    for name, fn in (("habits", _sync_habits), ("energy", _sync_jobs), ("briefing", _sync_pending)):
        try:
            fn(siris)
        except Exception:
            logger.debug("attention sync %s failed", name, exc_info=True)
    last = siris.attention._last_brain_sync
    if include_brain and siris.second_brain is not None and (last is None or time.monotonic() - last > BRAIN_SYNC_SECONDS):
        siris.attention._last_brain_sync = time.monotonic()
        try:
            _sync_brain(siris)
        except Exception:
            logger.debug("attention sync brain failed", exc_info=True)


def _sync_habits(siris: "SirisAI") -> None:
    from app.anticipation import habits

    siris.attention.sync(
        "habits",
        [
            {
                "key": f"habit:{s['id']}",
                "kind": "suggestion",
                "title": f"Automate this? {s.get('description') or s['id']}",
                "body": "You do this by hand at about the same time most days. I can set up a Home Assistant automation for it.",
                "severity": "info",
                "actions": [action("accept", "Create automation", "primary"), action("dismiss", "No thanks")],
                "payload": {"suggestion_id": s["id"]},
            }
            for s in habits.suggestions(siris)
        ],
        outcome="Handled elsewhere",
    )


def _sync_jobs(siris: "SirisAI") -> None:
    wanted = []
    for job in siris.deferred_jobs.list("waiting"):
        deadline = f" (by {job['deadline'][:16].replace('T', ' ')} at the latest)" if job.get("deadline") else ""
        wanted.append(
            {
                "key": f"job:{job['id']}",
                "kind": "waiting",
                "title": f"Waiting for cheap power: {job.get('reason') or job['tool_name']}",
                "body": f"{job['tool_name']} will run at the next cheap moment{deadline}.",
                "severity": "info",
                "actions": [action("cancel", "Cancel job", "danger", confirm="Cancel this job?")],
                "payload": {"job_id": job["id"]},
            }
        )
    siris.attention.sync("energy", wanted, outcome="Ran or was cancelled")


def _sync_pending(siris: "SirisAI") -> None:
    siris.attention.sync(
        "briefing",
        [
            {
                "key": f"pending:{p['id']}",
                "kind": "notice",
                "title": p["message"][:140],
                "body": p["message"],
                "severity": "info" if p.get("severity") != "warning" else "attention",
            }
            for p in siris.pending_notifications.list_pending()
        ],
        outcome="Delivered in the briefing",
    )


def _sync_brain(siris: "SirisAI") -> None:
    insights = siris.second_brain.insights(days=7)
    wanted = []
    pending = int(insights.get("pending_auto_links") or 0)
    unsure = len(insights.get("suggested_links") or [])
    if pending or unsure:
        parts = []
        if pending:
            parts.append(f"{pending} link{'s' if pending != 1 else ''} about to be made automatically")
        if unsure:
            parts.append(f"{unsure} pair{'s' if unsure != 1 else ''} that could be linked")
        wanted.append(
            {
                "key": "brain:links",
                "kind": "review",
                "title": "Siris links to review",
                "body": "; ".join(parts) + ".",
                "severity": "info",
                "link": "brain:links",
                "actions": [],
            }
        )
    inbox = int((insights.get("summary") or {}).get("inbox") or 0)
    if inbox:
        wanted.append(
            {
                "key": "brain:inbox",
                "kind": "review",
                "title": f"{inbox} note{'s' if inbox != 1 else ''} in Siris' inbox",
                "body": "New notes wait in 00-Inbox until they're sorted.",
                "severity": "info",
                "link": "brain:inbox",
                "actions": [],
            }
        )
    siris.attention.sync("second-brain", wanted, outcome="Reviewed")


# -- acting ---------------------------------------------------------------------


class UnknownAction(ValueError):
    pass


# (source, action id) -> handler(siris, item, payload, trigger) -> (result, outcome).
# Features with their own feed items (the learning loop's memory proposals,
# goal check-ins, updates...) register here instead of growing act() below.
_HANDLERS: dict[tuple[str, str], Any] = {}


def register_action(source: str, action_id: str, handler: Any) -> None:
    _HANDLERS[(source, action_id)] = handler


def _learn(siris: "SirisAI", item: dict[str, Any], action_id: str, payload: dict[str, Any]) -> None:
    try:
        from app.learning.observe import on_attention_action

        on_attention_action(siris, item, action_id, payload)
    except Exception:  # noqa: BLE001 - learning is never in the way of acting
        logger.debug("Recording the outcome of an attention action failed", exc_info=True)


def act(siris: "SirisAI", item_id: int, action_id: str, client: str = "the app", body: str | None = None) -> tuple[dict[str, Any], Any]:
    """Do what the action says, then close the item. Raises LookupError for
    an unknown or already-closed item and UnknownAction for an action the
    item doesn't offer."""
    item = siris.attention.get(item_id)
    if item is None or item["status"] != "open":
        raise LookupError(f"No open attention item {item_id}")
    if action_id not in {a["id"] for a in item["actions"]} and action_id != "dismiss":
        raise UnknownAction(f"Item {item_id} has no action '{action_id}'")
    payload = siris.attention._payload(item_id)
    if body is not None and item["source"] == "triage" and action_id == "send":
        if not body.strip():
            raise UnknownAction("An edited reply can't be empty")
        payload = {**payload, "body": body.strip()}
    trigger = f"attention feed: the user chose '{action_id}' on '{item['title']}' in {client}"
    result: Any = None

    _learn(siris, item, action_id, payload)
    if action_id in ("acknowledge", "decline"):
        closed = siris.attention.close(item_id, "done" if action_id == "acknowledge" else "dismissed", action_id.capitalize() + "d")
        return closed or item, None
    if action_id == "dismiss":
        if item["source"] == "habits" and payload.get("suggestion_id"):
            from app.anticipation import habits

            habits.set_status(siris, payload["suggestion_id"], "dismissed")
        closed = siris.attention.close(item_id, "dismissed", "Dismissed")
        return closed or item, None

    handler = _HANDLERS.get((item["source"], action_id))
    if handler is not None:
        result, outcome = handler(siris, item, payload, trigger)
    elif action_id == "approve" and item["kind"] == "confirm":
        result = siris.run_tool(payload["tool"], payload.get("arguments") or {}, confirmed=True, trigger=trigger)
        outcome = f"Approved and ran {payload['tool']}"
    elif action_id == "accept" and item["source"] == "habits":
        result = siris.run_tool("accept_habit_suggestion", {"suggestion_id": payload["suggestion_id"]}, confirmed=True, trigger=trigger)
        outcome = "Automation created"
    elif action_id == "cancel" and item["source"] == "energy":
        result = {"cancelled": siris.deferred_jobs.cancel(int(payload["job_id"]))}
        outcome = "Job cancelled"
    elif action_id == "lockdown":
        result = siris.run_tool("run_protocol", {"name": "lockdown"}, confirmed=True, trigger=trigger)
        outcome = "Lockdown run"
    else:
        raise UnknownAction(f"Item {item_id} has no action '{action_id}'")
    closed = siris.attention.close(item_id, "done", outcome)
    return closed or item, result
