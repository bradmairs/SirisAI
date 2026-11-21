"""Shopping and meals tools (7.49) -- see app/household/meals.py."""

from __future__ import annotations

from typing import Any

from app.plugins.base import Tool, ToolPermission


class MealContextTool(Tool):
    name = "meal_context"
    description = (
        "What's needed to suggest meals or plan the week: what's in the pantry/fridge, the shopping list, busy evenings, "
        "dietary preferences and recent meals. Use for 'what can I cook with what's in the fridge?', "
        "'dinner ideas', 'plan meals for the week'; then write the ideas or plan yourself."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"have": {"type": "string", "description": "Ingredients the user just said they have, comma-separated."}},
        "required": [],
    }

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, have: str = "", **kwargs: Any) -> Any:
        from app.household import meals

        return meals.context(self._siris, have)


class MealPlanSaveTool(Tool):
    name = "meal_plan_save"
    description = (
        "Save a meal plan and add the ingredients the user doesn't have to the shopping list. "
        "plan: [{day: 'Monday', meal: 'Chicken stir fry', ingredients: ['chicken', 'peppers', 'noodles']}]."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "plan": {"type": "array", "items": {"type": "object"}, "description": "One entry per meal: day, meal, ingredients."},
            "add_missing": {"type": "boolean", "description": "Add missing ingredients to the shopping list. Defaults to true."},
        },
        "required": ["plan"],
    }

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, plan: list | None = None, add_missing: bool = True, **kwargs: Any) -> Any:
        from app.household import meals

        return meals.save(self._siris, plan or [], add_missing)


class MealPlanTool(Tool):
    name = "meal_plan"
    description = "This week's saved meal plan ('what's for dinner?', 'what's on the meal plan')."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: Any) -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        from app.household import meals

        return self._siris.state.get(meals.PLAN_KEY) or {"plan": [], "note": "No meal plan saved yet."}


MEAL_TOOLS = (MealContextTool, MealPlanSaveTool, MealPlanTool)
