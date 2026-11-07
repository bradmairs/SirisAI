"""Tools for the things you own (app/things), people (app/people) and the
news (app/news), 5.0."""

from typing import TYPE_CHECKING, Any

from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class ThingsFindTool(Tool):
    name = "things_find"
    description = (
        "Things you own: when each was bought, the price, the warranty and days left on it, and where the manual is. For "
        "'where's the dishwasher manual', 'is the TV still under warranty', 'when did I buy the fridge'. Blank query lists all."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"query": {"type": "string", "description": "What to look for, e.g. 'dishwasher'."}}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, query: str = "", **kwargs: Any) -> Any:
        from app.things import service

        return service.find(self._siris, query, limit=20)


class ThingsAddTool(Tool):
    name = "things_add"
    description = (
        "Remember something you own: its name, and any of brand, model, room, date bought (YYYY-MM-DD), price, shop, "
        "warranty (warranty_months, or warranty_until as a date) and where the manual is. For 'I bought a new Bosch "
        "dishwasher today with a two-year warranty'."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "brand": {"type": "string"}, "model": {"type": "string"}, "room": {"type": "string"},
            "bought": {"type": "string", "description": "YYYY-MM-DD"}, "price": {"type": "number"}, "shop": {"type": "string"},
            "warranty_months": {"type": "integer"}, "warranty_until": {"type": "string", "description": "YYYY-MM-DD"},
            "manual": {"type": "string", "description": "A link, or where the paper one is kept."},
        },
        "required": ["name"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.things import service

        return service.add(self._siris, kwargs)


class PeopleUpcomingTool(Tool):
    name = "people_upcoming"
    description = (
        "Birthdays and anniversaries coming up, soonest first, with how old they're turning and gift ideas from your "
        "notes. For 'whose birthday is next', 'what should I get Dom'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "Optional: one person, for gift ideas."}}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, name: str = "", **kwargs: Any) -> Any:
        from app.people import service

        if name:
            people = [p for p in service.everyone(self._siris) if p["name"].lower().startswith(name.lower().split()[0])]
            return [{**p, "gift_ideas": service.gift_ideas(self._siris, p["name"])} for p in people]
        return service.everyone(self._siris)[:10]


class PeopleRememberTool(Tool):
    name = "people_remember"
    description = (
        "Remember someone's birthday (MM-DD, or YYYY-MM-DD when the year's known), anniversary, how they're related, "
        "or that you caught up with them today (caught_up: true). For 'Dom's birthday is 14 March', 'I had lunch with Sam today'."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "birthday": {"type": "string"}, "anniversary": {"type": "string"},
            "relation": {"type": "string"}, "notes": {"type": "string"}, "caught_up": {"type": "boolean"},
            "keep_in_touch_days": {"type": "integer", "description": "Remind if it's been longer than this."},
        },
        "required": ["name"],
    }

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, name: str = "", caught_up: bool = False, **kwargs: Any) -> Any:
        from app.people import service

        if caught_up:
            service.caught_up(self._siris, name)
        return service.add(self._siris, {"name": name, **kwargs})


class PeopleLastSeenTool(Tool):
    name = "people_last_seen"
    description = "When you last saw or caught up with someone, from the calendar and catch-ups you've mentioned. For 'when did I last see Dom'."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, name: str = "", **kwargs: Any) -> Any:
        from app.people import service

        return service.last_seen(self._siris, name)


class NewsBriefTool(Tool):
    name = "news_brief"
    description = "Today's news from the feeds you follow (SIRISAI_NEWS_FEEDS), on your topics, and new podcast episodes. For 'what's in the news'."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.news import service

        return {"stories": service.stories(self._siris), "episodes": service.episodes(self._siris)}
