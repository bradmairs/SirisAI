"""The attention feed out loud (6.38): "anything I need to know?"

attention_summary reads the open items, urgent first, numbered, each with
what can be done about it, and remembers the order. attention_act then
does "dismiss the second one" or "do the first one". Acknowledging,
dismissing or declining needs no confirmation and works by voice; any
other action (a reboot, installing updates, approving something) asks
first and is never done by voice: those are for the app, where the PIN
can be given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI

LAST_KEY = "attention:last_summary"
SAFE_ACTIONS = ("acknowledge", "dismiss", "decline")
_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"]


class AttentionSummaryTool(Tool):
    name = "attention_summary"
    description = (
        "What needs the user's attention, urgent things first, numbered, with what can be done about each: the attention "
        "feed (alerts, approvals waiting, suggestions). For 'anything I need to know?', 'what's going on?', 'any alerts?'. "
        "Read it out; then attention_act does 'dismiss the second one' or 'do the first one'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"limit": {"type": "integer", "description": "At most this many (default 5)"}}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, limit: int = 5, **kwargs: Any) -> Any:
        items = [i for i in self._siris.attention.list("open") if i["kind"] != "progress"]
        shown = items[: max(1, min(int(limit or 5), 8))]
        self._siris.state.set(LAST_KEY, [i["id"] for i in shown])
        if not items:
            return {"count": 0, "items": [], "text": "Nothing needs you right now."}
        urgent = sum(1 for i in items if i["severity"] == "urgent")
        lines = []
        for n, item in enumerate(shown):
            choices = [a["label"] for a in item.get("actions") or [] if a["id"] not in ("dismiss",)]
            lead = f"{_ORDINALS[n].capitalize()}" + (", urgent" if item["severity"] == "urgent" else "")
            lines.append(f"{lead}: {item['title']}." + (f" You can: {', '.join(choices)}." if choices else ""))
        head = f"{len(items)} thing{'s' if len(items) != 1 else ''} need{'' if len(items) != 1 else 's'} you" + (f", {urgent} urgent" if urgent else "") + "."
        more = f" And {len(items) - len(shown)} more in the app." if len(items) > len(shown) else ""
        out = [{"number": n + 1, "id": i["id"], "title": i["title"], "severity": i["severity"], "source": i["source"],
                "actions": [{"id": a["id"], "label": a["label"]} for a in i.get("actions") or []]} for n, i in enumerate(shown)]
        return {"count": len(items), "urgent": urgent, "items": out,
                "text": f"{head} " + " ".join(lines) + more + " Say which one to dismiss, or do it in the app."}


class AttentionActTool(Tool):
    name = "attention_act"
    description = (
        "Act on an item from attention_summary by its number (1 = the first read out): action is one of its actions "
        "('acknowledge', 'dismiss', or e.g. 'reboot', 'install'). Acknowledging or dismissing just happens; anything else "
        "asks first and isn't done by voice."
    )
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = {
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "The item's number from attention_summary"},
            "action": {"type": "string", "description": "acknowledge, dismiss, or one of the item's actions"},
        },
        "required": ["number", "action"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def needs_confirmation(self, arguments: dict[str, Any]) -> bool:
        return str(arguments.get("action") or "").lower() not in SAFE_ACTIONS

    def _item(self, number: int) -> dict[str, Any]:
        ids = self._siris.state.get(LAST_KEY) or []
        if not 1 <= int(number) <= len(ids):
            raise ValueError("Ask what needs attention first, then pick one by its number.")
        item = self._siris.attention.get(ids[int(number) - 1])
        if item is None or item["status"] != "open":
            raise ValueError("That one has already been dealt with.")
        return item

    def run(self, number: int = 1, action: str = "dismiss", **kwargs: Any) -> Any:
        from app.hub import attention

        item = self._item(number)
        wanted = str(action).strip().lower()
        by_label = {a["label"].lower(): a["id"] for a in item.get("actions") or []}
        action_id = by_label.get(wanted, wanted)
        if action_id in ("do it", "yes", "ok") and item.get("actions"):
            action_id = item["actions"][0]["id"]
        closed, result = attention.act(self._siris, item["id"], action_id, client="chat or voice")
        outcome = (closed or {}).get("outcome") or "Done"
        return {"item": item["title"], "action": action_id, "outcome": outcome, "result": result,
                "message": f"{outcome}: {item['title']}."}
