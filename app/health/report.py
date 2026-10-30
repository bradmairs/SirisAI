"""A printable health summary for a doctor (GET /siris/health/report): the
period's averages and ranges for sleep, heart and vitals, the weight trend,
activity, and the symptoms and medications logged. Plain HTML that prints
cleanly; "Print → Save as PDF" in the browser makes the PDF."""

from __future__ import annotations

from datetime import timedelta
from html import escape
from statistics import mean
from typing import TYPE_CHECKING

from app.health import overview, scores

if TYPE_CHECKING:
    from app.core.siris import SirisAI

ROWS = [
    ("sleep_hours", "Sleep", "h", 1),
    ("resting_heart_rate", "Resting heart rate", "bpm", 0),
    ("heart_rate_variability", "HRV", "ms", 0),
    ("walking_heart_rate", "Walking heart rate", "bpm", 0),
    ("respiratory_rate", "Respiratory rate", "/min", 1),
    ("blood_oxygen", "Blood oxygen", "%", 0),
    ("wrist_temperature", "Wrist temperature (vs baseline)", "°C", 2),
    ("vo2_max", "VO₂ max", "ml/kg/min", 1),
    ("weight_kg", "Weight", "kg", 1),
    ("body_fat_pct", "Body fat", "%", 1),
    ("steps", "Steps a day", "", 0),
    ("exercise_minutes", "Exercise a day", "min", 0),
]


def html(siris: "SirisAI", days: int = 90) -> str:
    on = overview.today(siris)
    data = siris.health.days((on - timedelta(days=days)).isoformat(), on.isoformat())
    rows = []
    for key, label, unit, places in ROWS:
        values = [m[key] for m in data.values() if m.get(key) is not None]
        if not values:
            continue
        fmt = f"{{:,.{places}f}}"
        rows.append(f"<tr><td>{escape(label)}</td><td>{fmt.format(mean(values))} {unit}</td>"
                    f"<td>{fmt.format(min(values))} – {fmt.format(max(values))}</td><td>{len(values)}</td></tr>")
    weight = scores.weight_trend(data, on, None)
    workouts = siris.health.workouts((on - timedelta(days=days)).isoformat())
    notes = [n for n in siris.health.notes((on - timedelta(days=days)).isoformat()) if n["kind"] in ("symptom", "medication", "supplement", "feeling")]
    note_rows = "".join(f"<tr><td>{escape(n['day'])}</td><td>{escape(n['kind'])}</td><td>{escape(n['text'])}</td></tr>" for n in notes[:100])
    weight_line = (f"<p>Weight trend: {weight['trend']:.1f} kg, {weight['per_week']:+.2f} kg a week over the last four weeks.</p>" if weight else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Health summary</title>
<style>body{{font:14px system-ui,sans-serif;max-width:800px;margin:24px auto;color:#111}}table{{border-collapse:collapse;width:100%;margin:8px 0 20px}}
td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left}}h1{{font-size:20px}}h2{{font-size:16px;margin-top:24px}}.muted{{color:#666}}</style></head>
<body><h1>Health summary</h1>
<p class="muted">The last {days} days, to {on.isoformat()}. From Apple Health (via SirisAI); daily values, so averages are of days with data.</p>
<h2>Averages and ranges</h2>
<table><tr><th>Measure</th><th>Average</th><th>Range</th><th>Days</th></tr>{''.join(rows) or '<tr><td colspan="4">No data in this period.</td></tr>'}</table>
{weight_line}
<p>Workouts: {len(workouts)} in this period, {sum(w['minutes'] or 0 for w in workouts):.0f} minutes in total.</p>
<h2>Symptoms and medications logged</h2>
<table><tr><th>Day</th><th>Kind</th><th>Note</th></tr>{note_rows or '<tr><td colspan="3">None logged.</td></tr>'}</table>
<p class="muted">Not a medical record. Consumer wearables can be wrong; use this as a conversation starter.</p>
</body></html>"""
