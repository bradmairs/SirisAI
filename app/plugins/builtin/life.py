"""Life admin tools (4.6): the document inbox (app/documents), bills and
subscriptions from email (app/bills), and trips in the calendar
(app/travel)."""

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class DocumentsSearchTool(Tool):
    name = "documents_search"
    description = (
        "Letters, bills and forms added to the document inbox, newest first, with who sent each, a summary and what's "
        "due. For 'what did the council letter say', 'when is the rego due', 'find the insurance renewal'. Give "
        "`id` to read one in full."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for (blank: the latest ones)."},
            "id": {"type": "integer", "description": "One document's id, to read its full text."},
        },
        "required": [],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, query: str = "", id: int | None = None, **kwargs: Any) -> Any:
        from app.documents import service

        if id:
            return service.get(self._siris, int(id))
        return service.list_documents(self._siris, query, limit=10)


class BillsUpcomingTool(Tool):
    name = "bills_upcoming"
    description = (
        "Subscriptions renewing, free trials ending, price rises and bills due, found in email, soonest first. For "
        "'what's renewing soon', 'any bills due', 'did anything go up in price'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.bills import service

        return [{k: b[k] for k in ("label", "kind", "service", "amount", "due_date")} for b in service.upcoming(self._siris)]


class TravelUpcomingTool(Tool):
    name = "travel_upcoming"
    description = (
        "Trips in the calendar over the next three weeks: where, when, how many nights, the weather there, what to "
        "pack and what to sort at home. For 'when am I away next', 'what should I pack for Sydney'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.travel import service

        return service.upcoming(self._siris)
