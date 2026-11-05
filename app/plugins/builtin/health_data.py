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
    note = siris.health.add_note("meal", f"{description.strip()[:200]} (about {kcal * 4.184:,.0f} kJ" + (f", {protein_g:.0f} g protein)" if protein_g else ")"), day)
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


class HealthHistoryTool(_HealthTool):
    name = "health_history"
    description = (
        "The user's health over any stretch of days, against the 30 days before it: sleep, overnight HRV, resting heart rate, "
        "steps, exercise, weight and the rest (average, lowest, highest, change), the morning readiness scores kept for those days, "
        "and the workouts. For questions about the past: 'how did I sleep the week before the deadline', 'was my HRV down in "
        "September', 'how much did I run in the fortnight after the race'. Find the dates first (calendar, notes) when the "
        "question names an event, then pass start and end."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "First day, YYYY-MM-DD."},
            "end": {"type": "string", "description": "Last day, YYYY-MM-DD (default: start)."},
            "metrics": {"type": "string", "description": "Comma-separated metrics to keep it short (e.g. sleep_hours,hrv_overnight); default: all."},
        },
        "required": ["start"],
    }

    def run(self, start: str = "", end: str = "", metrics: str = "", **kwargs: Any) -> Any:
        from datetime import date as date_type
        from statistics import mean

        from app.core.user_context import get_current_user_id

        first = date_type.fromisoformat(start[:10])
        last = date_type.fromisoformat((end or start)[:10])
        if last < first:
            first, last = last, first
        if (last - first).days > 400:
            raise ValueError("Ask about a year or less at a time.")
        before = first - timedelta(days=30)
        days = self._siris.health.days(before.isoformat(), last.isoformat())
        wanted = {m.strip() for m in metrics.split(",") if m.strip()}
        period: dict[str, list[float]] = {}
        usual: dict[str, list[float]] = {}
        for d, ms in days.items():
            into = period if d >= first.isoformat() else usual
            for k, v in ms.items():
                if not wanted or k in wanted:
                    into.setdefault(k, []).append(float(v))
        out = {}
        for k, vals in sorted(period.items()):
            b = usual.get(k)
            avg = mean(vals)
            out[k] = {"average": round(avg, 1), "lowest": round(min(vals), 1), "highest": round(max(vals), 1), "days": len(vals),
                      "usual_before": round(mean(b), 1) if b and len(b) >= 5 else None,
                      "change_pct": round((avg - mean(b)) / mean(b) * 100, 1) if b and len(b) >= 5 and mean(b) else None}
        kept = self._siris.state.get(f"health_readiness:{get_current_user_id()}") or {}
        readiness = {d: r["score"] for d, r in sorted(kept.items()) if first.isoformat() <= d <= last.isoformat()}
        workouts = [w for w in self._siris.health.workouts(first.isoformat()) if w["date"] <= last.isoformat()]
        return {"from": first.isoformat(), "to": last.isoformat(), "metrics": out, "readiness_by_day": readiness,
                "workouts": workouts[:30], "note": "usual_before is the 30 days before the period; nothing for days with no data."}


class HealthExplainTool(_HealthTool):
    name = "health_explain"
    description = (
        "Why the user's readiness was low on recent mornings: each morning's score, what pulled the low ones down "
        "(sleep, overnight HRV, resting heart rate), and what the evening before had (a late workout or meeting, "
        "caffeine, alcohol, a warm bedroom, late TV, a hard day). For 'why was my readiness low this week', "
        "'why am I so flat', 'what changed'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"days": {"type": "integer", "description": "How many mornings back (default 7)"}}, "required": []}

    def run(self, days: int = 7, **kwargs: Any) -> Any:
        return insights.explain(self._siris, days=max(2, min(int(days or 7), 30)))


class GymProgressTool(_HealthTool):
    name = "gym_progress"
    description = (
        "The user's strength training in SirisGym (SirisOS): each main lift's estimated one-rep max over recent sessions, "
        "this week's sets by muscle against a target, records, whether a deload week is due, and the next routine day "
        "with the weights to lift. For 'how's my bench trending', 'what should I do in the gym today', 'am I training legs "
        "enough', 'did I set any PRs'. Pass exercise for one lift."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"exercise": {"type": "string", "description": "A lift, e.g. bench, squat, deadlift"}}, "required": []}

    def run(self, exercise: str | None = None, **kwargs: Any) -> Any:
        from app.health import gym

        s = gym.latest(self._siris)
        if not s:
            return {"text": "Nothing from SirisGym yet: it sends a summary after each workout (SirisOS 6.9 or later)."}
        return {"text": gym.text(s, exercise), "as_of": s.get("received_at"), "lifts": s.get("lifts"), "next": s.get("next"),
                "week": s.get("week"), "deload": s.get("deload")}


HEALTH_TOOLS = (HealthOverviewTool, GymProgressTool, HealthExplainTool, HealthHistoryTool, HealthInsightsTool, HealthLogTool, HealthLogMealTool, HealthSetGoalTool, HealthGoalsTool)
