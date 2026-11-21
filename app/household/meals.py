"""Shopping and meals (7.49).

The model writes the recipes and plans itself; these tools give it what it
needs and act on the result:

- **meal_context**: what's in (a Home Assistant to-do list named for the
  pantry or fridge, SIRISAI_PANTRY_LIST, plus anything you list), what's
  already on the shopping list, the week's evenings (busy ones from the
  calendar), your dietary notes
  (SIRISAI_MEAL_PREFERENCES) and what you've had lately -- for "what can I
  cook with what's in the fridge?" and "plan meals for the week".
- **meal_plan_save**: keeps the plan, and adds every ingredient you don't
  have to the shopping list (SIRISAI_SHOPPING_LIST, else the list called
  shopping) -- the "missing ingredients straight to the list" step.
- **meal_plan**: this week's plan; the brief says what's for dinner tonight.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
PLAN_KEY = "meals:plan"
HISTORY_KEY = "meals:history"
_PANTRY_WORDS = ("pantry", "fridge", "freezer", "cupboard", "in stock", "kitchen stock")


def _ha(siris: "SirisAI") -> Any:
    return getattr(siris, "_home_assistant_client", None)


def _lists(siris: "SirisAI") -> list[dict[str, Any]]:
    client = _ha(siris)
    if client is None:
        return []
    out = []
    for s in client.list_states(domain="todo"):
        name = ((s.get("attributes") or {}).get("friendly_name") or s["entity_id"]).lower()
        out.append({"entity_id": s["entity_id"], "name": name})
    return out


def _find_list(siris: "SirisAI", setting: str, words: tuple[str, ...]) -> str | None:
    chosen = (setting or "").strip()
    lists = _lists(siris)
    if chosen:
        return next((l["entity_id"] for l in lists if chosen.lower() in (l["entity_id"].lower(), l["name"])), chosen if chosen.startswith("todo.") else None)
    return next((l["entity_id"] for l in lists if any(w in l["name"] or w in l["entity_id"] for w in words)), None)


def _items(siris: "SirisAI", entity_id: str | None) -> list[str]:
    if not entity_id or _ha(siris) is None:
        return []
    try:
        return [str(i.get("summary") or "") for i in _ha(siris).get_todo_items(entity_id) if i.get("summary")]
    except Exception:
        return []


def _week(siris: "SirisAI", today: date) -> list[dict[str, Any]]:
    days = [{"date": (today + timedelta(days=i)).isoformat(), "day": (today + timedelta(days=i)).strftime("%A")} for i in range(7)]
    if siris.tools.get("calendar_upcoming_events") is not None:
        try:
            events = siris.run_tool("calendar_upcoming_events", {"days_ahead": 7}, trigger="meals: the week's evenings")
            for d in days:
                evening = [e.get("summary") or e.get("title") for e in events or []
                           if str(e.get("start", ""))[:10] == d["date"] and str(e.get("start", ""))[11:13] >= "17"]
                if evening:
                    d["evening"] = evening
        except Exception:
            pass
    return days


def context(siris: "SirisAI", have: str = "", now: datetime | None = None) -> dict[str, Any]:
    today = (now or datetime.now(UTC)).date()
    pantry = _find_list(siris, siris.settings.pantry_list, _PANTRY_WORDS)
    shopping = _find_list(siris, siris.settings.shopping_list, ("shopping", "groceries", "grocery"))
    in_stock = _items(siris, pantry) + [h.strip() for h in have.split(",") if h.strip()]
    return {
        "have": in_stock,
        "pantry_list": pantry,
        "already_on_shopping_list": _items(siris, shopping),
        "shopping_list": shopping,
        "week": _week(siris, today),
        "preferences": siris.settings.meal_preferences or None,
        "recent_meals": [m["meal"] for m in (siris.state.get(HISTORY_KEY) or [])[-10:]],
        "note": None if in_stock else "Nothing listed as in stock: ask what's in, or suggest from a typical pantry.",
    }


def save(siris: "SirisAI", plan: list[dict[str, Any]], add_missing: bool = True) -> dict[str, Any]:
    if not plan:
        raise ValueError("Give the plan as a list of {day, meal, ingredients}.")
    have = {i.lower() for i in _items(siris, _find_list(siris, siris.settings.pantry_list, _PANTRY_WORDS))}
    shopping = _find_list(siris, siris.settings.shopping_list, ("shopping", "groceries", "grocery"))
    listed = {i.lower() for i in _items(siris, shopping)}
    clean = [{"day": str(p.get("day") or ""), "meal": str(p.get("meal") or "").strip(), "ingredients": [str(i).strip() for i in p.get("ingredients") or [] if str(i).strip()]}
             for p in plan if p.get("meal")]
    siris.state.set(PLAN_KEY, {"saved": datetime.now(UTC).date().isoformat(), "plan": clean})
    history = list(siris.state.get(HISTORY_KEY) or [])
    siris.state.set(HISTORY_KEY, (history + [{"meal": p["meal"]} for p in clean])[-40:])
    added: list[str] = []
    if add_missing and shopping and _ha(siris) is not None:
        needed = dict.fromkeys(i for p in clean for i in p["ingredients"]
                               if not any(i.lower() in h or h in i.lower() for h in have) and i.lower() not in listed)
        for item in needed:
            try:
                _ha(siris).add_todo_item(shopping, item)
                added.append(item)
            except Exception:
                logger.info("Couldn't add %s to the shopping list", item, exc_info=True)
    return {"meals": len(clean), "added_to_shopping_list": added, "shopping_list": shopping}


def tonight(siris: "SirisAI", now: datetime | None = None) -> str | None:
    found = siris.state.get(PLAN_KEY) or {}
    day = (now or datetime.now(UTC)).strftime("%A").lower()
    meal = next((p for p in found.get("plan") or [] if p["day"].lower().startswith(day[:3])), None)
    return f"Dinner tonight: {meal['meal']}." if meal else None
