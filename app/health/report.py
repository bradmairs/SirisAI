"""A printable health summary for a doctor (GET /siris/health/report): the
period's averages and ranges for sleep, heart and vitals, the weight trend,
activity, and the symptoms and medications logged. Plain HTML that prints
cleanly; "Print → Save as PDF" in the browser makes the PDF.

7.28: also each month side by side (sleep, heart, weight, activity, mood,
alcohol), and "Worth mentioning": what's trending the wrong way over the last
30 days against the time before (sleep down half an hour, resting heart rate
up 3 bpm, HRV down 15%, weight changed 2 kg, much less active, mood lower,
blood oxygen under 95%, more than 10 drinks a week). Fits one A4 page."""

from __future__ import annotations

from datetime import timedelta
from html import escape
from datetime import date
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


MONTH_ROWS = [
    ("sleep_hours", "Sleep", "{:.1f} h"),
    ("resting_heart_rate", "Resting HR", "{:.0f} bpm"),
    ("heart_rate_variability", "HRV", "{:.0f} ms"),
    ("weight_kg", "Weight", "{:.1f} kg"),
    ("steps", "Steps a day", "{:,.0f}"),
    ("exercise_minutes", "Exercise a day", "{:.0f} min"),
    ("mood", "Mood", "{}"),
    ("alcohol_week", "Drinks a week", "{:.1f}"),
]
RECENT_DAYS = 30
MIN_DAYS = 7


def mood_of(m: dict) -> float | None:
    """One mood scale (-1 to 1): Apple's State of Mind, else the 1-5 quick log."""
    if m.get("mood_valence") is not None:
        return float(m["mood_valence"])
    if m.get("mood") is not None:
        return (float(m["mood"]) - 3) / 2
    return None


def mood_word(v: float) -> str:
    return "good" if v >= 0.35 else "fairly good" if v >= 0.1 else "mixed" if v > -0.1 else "low" if v > -0.35 else "very low"


def _avg(data: dict[str, dict], key: str, days: set[str] | None = None) -> tuple[float | None, int]:
    if key == "mood":
        vals = [v for d, m in data.items() if (days is None or d in days) and (v := mood_of(m)) is not None]
    elif key == "alcohol_week":
        picked = [m for d, m in data.items() if days is None or d in days]
        logged = [m.get("alcohol_drinks") or 0 for m in picked if any(k in m for k in ("alcohol_drinks", "water_ml", "caffeine_mg"))]
        return (sum(logged) / len(logged) * 7 if logged else None), len(logged)
    else:
        vals = [m[key] for d, m in data.items() if (days is None or d in days) and m.get(key) is not None]
    return (mean(vals) if vals else None), len(vals)


def months(data: dict[str, dict], on: date, count: int) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Each calendar month in the period (the latest `count`), and each measure's average for it."""
    ms = sorted({d[:7] for d in data})[-count:]
    rows = []
    for key, label, fmt in MONTH_ROWS:
        cells = []
        for ym in ms:
            v, n = _avg(data, key, {d for d in data if d.startswith(ym)})
            cells.append("–" if v is None else mood_word(v) if key == "mood" else fmt.format(v))
        if any(c != "–" for c in cells):
            rows.append((label, cells))
    return ms, rows


def worth_mentioning(data: dict[str, dict], on: date) -> list[str]:
    """What's going the wrong way: the last 30 days against everything before them in the period."""
    cut = (on - timedelta(days=RECENT_DAYS)).isoformat()
    recent = {d for d in data if d > cut}
    before = {d for d in data if d <= cut}
    out = []

    def pair(key: str) -> tuple[float | None, float | None]:
        a, na = _avg(data, key, before)
        b, nb = _avg(data, key, recent)
        return (a, b) if na >= MIN_DAYS and nb >= MIN_DAYS else (None, None)

    a, b = pair("sleep_hours")
    lately, n = _avg(data, "sleep_hours", recent)
    if a is not None and b <= a - 0.5:
        out.append(f"Sleep is down: {b:.1f} h a night in the last month against {a:.1f} h before.")
    elif lately is not None and n >= MIN_DAYS and lately < 6.5:
        out.append(f"Sleep has averaged {lately:.1f} h a night in the last month.")
    a, b = pair("resting_heart_rate")
    if a is not None and b >= a + 3:
        out.append(f"Resting heart rate is up: {b:.0f} bpm in the last month against {a:.0f} bpm before.")
    a, b = pair("heart_rate_variability")
    if a is not None and b <= a * 0.85:
        out.append(f"HRV is down {100 - 100 * b / a:.0f}%: {b:.0f} ms against {a:.0f} ms before.")
    a, b = pair("weight_kg")
    if a is not None and abs(b - a) >= 2:
        out.append(f"Weight is {'up' if b > a else 'down'} {abs(b - a):.1f} kg: {b:.1f} kg in the last month against {a:.1f} kg before.")
    a, b = pair("steps")
    if a is not None and b <= a * 0.75:
        out.append(f"Less active: {b:,.0f} steps a day in the last month against {a:,.0f} before.")
    a, b = pair("exercise_minutes")
    if a is not None and b <= a * 0.7 and a - b >= 10:
        out.append(f"Less exercise: {b:.0f} minutes a day against {a:.0f} before.")
    a, b = pair("mood")
    if a is not None and b <= a - 0.2:
        out.append(f"Mood is lower: mostly {mood_word(b)} in the last month against {mood_word(a)} before.")
    b, n = _avg(data, "blood_oxygen", recent)
    if b is not None and n >= MIN_DAYS and b < 95:
        out.append(f"Blood oxygen has averaged {b:.0f}% in the last month.")
    a, b = pair("respiratory_rate")
    if a is not None and b >= a + 2:
        out.append(f"Breathing rate is up: {b:.1f} a minute against {a:.1f} before.")
    b, n = _avg(data, "alcohol_week", recent)
    if b is not None and n >= MIN_DAYS and b > 10:
        out.append(f"About {b:.0f} drinks a week in the last month (the Australian guideline is no more than 10).")
    return out


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
    ms, mrows = months(data, on, 6)
    month_table = ""
    if len(ms) > 1 and mrows:
        head = "".join(f"<th>{date.fromisoformat(m + '-01'):%b %Y}</th>" for m in ms)
        body = "".join(f"<tr><td>{escape(label)}</td>{''.join(f'<td>{escape(c)}</td>' for c in cells)}</tr>" for label, cells in mrows)
        month_table = f"<h2>Month by month</h2><table><tr><th>Measure</th>{head}</tr>{body}</table>"
    flags = worth_mentioning(data, on)
    flag_html = ("<ul>" + "".join(f"<li>{escape(f)}</li>" for f in flags) + "</ul>") if flags else \
        "<p>Nothing stands out: sleep, heart, weight, activity and mood are about where they were.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Health summary</title>
<style>body{{font:14px system-ui,sans-serif;max-width:800px;margin:24px auto;color:#111}}table{{border-collapse:collapse;width:100%;margin:8px 0 20px}}
td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left}}h1{{font-size:20px}}h2{{font-size:16px;margin-top:24px}}.muted{{color:#666}}
.flags{{border:1px solid #e0a800;border-radius:6px;padding:4px 14px;background:#fffaf0}}
@page{{size:A4;margin:12mm}}@media print{{body{{font-size:11px;margin:0;max-width:none}}td,th{{padding:3px 5px}}table{{margin:4px 0 10px}}h2{{margin-top:12px}}}}</style></head>
<body><h1>Health summary</h1>
<p class="muted">The last {days} days, to {on.isoformat()}. From Apple Health (via SirisAI); daily values, so averages are of days with data.</p>
<h2>Worth mentioning</h2><div class="flags">{flag_html}</div>
<h2>Averages and ranges</h2>
<table><tr><th>Measure</th><th>Average</th><th>Range</th><th>Days</th></tr>{''.join(rows) or '<tr><td colspan="4">No data in this period.</td></tr>'}</table>
{weight_line}
{month_table}
<p>Workouts: {len(workouts)} in this period, {sum(w['minutes'] or 0 for w in workouts):.0f} minutes in total.</p>
<h2>Symptoms and medications logged</h2>
<table><tr><th>Day</th><th>Kind</th><th>Note</th></tr>{note_rows or '<tr><td colspan="3">None logged.</td></tr>'}</table>
<p class="muted">Not a medical record. Consumer wearables can be wrong; use this as a conversation starter.</p>
</body></html>"""
