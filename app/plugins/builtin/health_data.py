"""Health tools (app/health/): the summary and the full overview (readiness,
sleep, load, vitals, weight), what sleep goes with, tonight's bedtime, quick
logs by voice ("log 500 ml water", "took my vitamins", "headache since
lunch"), meal estimates, and goals tied to a health number."""

from typing import TYPE_CHECKING, Any

from datetime import timedelta

from app.core import clock
from app.health import insights, overview
from app.health.service import summary
from app.plugins.base import Tool, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI


class HealthSummaryTool(Tool):
    name = "health_summary"
    description = (
        "The user's recent health data from their phone -- sleep, steps, resting heart rate, HRV, activity, weight -- "
        "with the latest day compared to their own usual. For 'how did I sleep', 'am I moving enough this week'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def run(self, **kwargs: Any) -> Any:
        return summary(self._siris.health, clock.now_local(self._siris.settings.timezone).date())


class _HealthTool(Tool):
    def __init__(self, siris: "SirisAI") -> None:
        self._siris = siris

    def _today(self) -> str:
        return clock.now_local(self._siris.settings.timezone).date().isoformat()


class HealthOverviewTool(_HealthTool):
    name = "health_overview"
    description = (
        "Today's readiness (0-100: primed, steady or recover, with advice), last night's sleep (hours, score, stages, "
        "debt), training load, vitals out of their usual range, the weight trend, and whether the body seems to be "
        "fighting something off. For 'how ready am I', 'should I train hard today', 'how did I sleep', 'am I getting sick'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        built = overview.build(self._siris)
        return {"text": overview.text(built), "readiness": built["readiness"], "sleep": {k: v for k, v in built["sleep"].items() if k != "nights"},
                "load": built["activity"]["load"], "vitals_out_of_range": [v for v in built["vitals"] if v["state"] != "normal"],
                "weight": {k: v for k, v in (built["weight"] or {}).items() if k != "series"} or None, "getting_sick": built["getting_sick"],
                "workouts_recent": built["activity"]["workouts"][:5]}


class HealthInsightsTool(_HealthTool):
    name = "health_insights"
    description = (
        "What the user's sleep goes with, from their own nights: late meetings, late workouts, caffeine, alcohol, a warm "
        "bedroom, late TV. For 'why am I sleeping badly', 'what affects my sleep'. Also tonight's suggested bedtime."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        found = insights.compute(self._siris)
        plan = insights.bedtime(self._siris)
        text = " ".join(f["text"] for f in found) or "Nothing stands out yet: it needs a few weeks of nights with and without each thing."
        return {"text": text + (" " + plan["text"] if plan else ""), "insights": found, "bedtime": plan}


LOG_KINDS = {
    "water": ("water_ml", "ml"), "caffeine": ("caffeine_mg", "mg"), "alcohol": ("alcohol_drinks", "drinks"),
    "mood": ("mood", "1-5"), "energy": ("energy_level", "1-5"),
}
NOTE_KINDS = ("symptom", "supplement", "medication", "feeling", "note")


class HealthLogTool(_HealthTool):
    name = "health_log"
    description = (
        "Log something health-related the user just said: water ('500 ml of water'), caffeine ('a coffee' = 80 mg), "
        "alcohol (drinks), mood or energy (1-5), or a note: a symptom ('headache since lunch'), a supplement or "
        "medication taken ('took my vitamins'), or how they feel. Amounts add up over the day."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [*LOG_KINDS, *NOTE_KINDS]},
            "amount": {"type": "number", "description": "For water (ml), caffeine (mg), alcohol (drinks), mood or energy (1-5)."},
            "text": {"type": "string", "description": "For a symptom, supplement, medication, feeling or note: in the user's words."},
        },
        "required": ["kind"],
    }

    def run(self, kind: str = "", amount: float | None = None, text: str = "", **kwargs: Any) -> Any:
        day = self._today()
        if kind in LOG_KINDS:
            if amount is None:
                raise ValueError(f"How much? ({LOG_KINDS[kind][1]})")
            metric, unit = LOG_KINDS[kind]
            current = dict(self._siris.health.series(metric, day)).get(day, 0.0)
            value = float(amount) if kind in ("mood", "energy") else current + float(amount)
            self._siris.health.record({metric: value}, day, source="manual")
            return {"logged": kind, "amount": amount, "unit": unit, "today_total": value}
        if kind in NOTE_KINDS:
            if not text.strip():
                raise ValueError("What should I note?")
            return {"logged": kind, **self._siris.health.add_note(kind, text.strip(), day)}
        raise ValueError(f"kind must be one of {', '.join([*LOG_KINDS, *NOTE_KINDS])}")


class HealthLogMealTool(_HealthTool):
    name = "health_log_meal"
    description = (
        "Log a meal with a rough calorie and protein estimate, e.g. from a photo the user sent (the image description "
        "is in the conversation) or their description. Always a rough estimate: say so."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What the meal was."},
            "kcal": {"type": "number", "description": "Estimated calories."},
            "protein_g": {"type": "number", "description": "Estimated protein in grams."},
        },
        "required": ["description", "kcal"],
    }

    def run(self, description: str = "", kcal: float = 0, protein_g: float | None = None, **kwargs: Any) -> Any:
        return log_meal(self._siris, description, kcal, protein_g, self._today())


def log_meal(siris: "SirisAI", description: str, kcal: float, protein_g: float | None, day: str) -> dict[str, Any]:
    totals = {}
    for metric, add in (("meal_kcal_estimate", kcal), ("meal_protein_g_estimate", protein_g)):
        if add:
            current = dict(siris.health.series(metric, day)).get(day, 0.0)
            totals[metric] = current + float(add)
    if totals:
        siris.health.record(totals, day, source="manual")
    note = siris.health.add_note("meal", f"{description.strip()[:200]} (about {kcal:.0f} kcal" + (f", {protein_g:.0f} g protein)" if protein_g else ")"), day)
    return {"logged": "meal", "estimate": True, "kcal": kcal, "protein_g": protein_g, "today_kcal_estimate": totals.get("meal_kcal_estimate"), "note": note}


class HealthSetGoalTool(_HealthTool):
    name = "health_set_goal"
    description = (
        "Set a goal tied to a health number, tracked from the data: 'run 1000 km this year', '150 minutes of exercise a "
        "week until Christmas', '10,000 steps a day in November' (as a total). metric is running_km, walk_run_km, "
        "exercise_minutes, steps, active_energy_kcal or mindful_minutes; target is the total over start..end."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "enum": list(insights.GOAL_METRICS)},
            "target": {"type": "number"},
            "start": {"type": "string", "description": "YYYY-MM-DD (default today)."},
            "end": {"type": "string", "description": "YYYY-MM-DD."},
            "title": {"type": "string"},
        },
        "required": ["metric", "target", "end"],
    }

    def run(self, metric: str = "", target: float = 0, start: str = "", end: str = "", title: str = "", **kwargs: Any) -> Any:
        return insights.set_goal(self._siris, metric, float(target), start or self._today(), end, title or None)


class HealthGoalsTool(_HealthTool):
    name = "health_goals"
    description = "Progress on goals tied to health numbers (distance run this year and so on): done so far, on pace or not."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs: Any) -> Any:
        return insights.goals_progress(self._siris)


HEALTH_TOOLS = (HealthOverviewTool, HealthInsightsTool, HealthLogTool, HealthLogMealTool, HealthSetGoalTool, HealthGoalsTool)
