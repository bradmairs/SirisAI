"""CPD and chartership evidence that records itself as it happens.

Engineers Australia counts CPD over three years, and chartership (CPEng)
needs evidence against the Stage 2 competency elements. Both are easy to
let slide. Each evening (SIRISAI_CAREER_CAPTURE_HOUR_LOCAL, 6pm) SirisAI
looks back over the day:

- **CPD.** Calendar events that look like professional development
  (webinars, courses, conferences, training, site inspections, technical
  talks...) become "Log as CPD?" items in the attention feed. Each comes with
  the hours from the event's length and a category guessed from its title:
  risk management, business and management, or area of practice. "Add to
  CPD" keeps a draft entry. You can also log one in chat ("log 2 hours of
  CPD for the drainage webinar", `career_log_cpd`).
- **Chartership evidence.** A day where SirisAI did engineering work with
  you (calculations, calc notes, standards questions) becomes "Chartership
  evidence?" with a short summary of what was done. When SirisOS knows
  which competency elements still need evidence, it suggests the ones that
  fit. "Keep as evidence" saves it.

Nothing is sent to Engineers Australia: CPD is logged there, so the drafts
export as CSV (`GET /siris/career/drafts?format=csv`), ready to copy across.
Kept drafts are also written to the Second Brain when it's configured.
"""

import csv
import io
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core import clock
from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

SOURCE = "career"
CPD_KEY = "career_cpd_drafts"
EVIDENCE_KEY = "career_evidence_drafts"

CPD_RE = re.compile(
    r"\b(webinar|seminar|course|training|workshop|conference|symposium|cpd|lecture|technical (talk|session|presentation)|"
    r"masterclass|short course|toolbox|site (visit|inspection)|engineers australia|ipwea|stormwater (victoria|australia)|awa|"
    r"lunch (and|&) learn|learning session|certification|induction)\b",
    re.IGNORECASE,
)
_RISK_RE = re.compile(r"\b(risk|safety|ohs|whs|hazard|safe design|incident|audit)\b", re.IGNORECASE)
_BUSINESS_RE = re.compile(r"\b(management|leadership|contract|procurement|business|finance|project management|commercial|negotiat)", re.IGNORECASE)
ENGINEERING_TOOLS = ("engineering_calculate", "engineering_calc_note", "sirishydro_ask", "engineering_standards_search")
_ELEMENT_WORDS = {
    "engineering_calculate": ("design", "solution", "analysis", "technical", "problem", "engineering"),
    "engineering_calc_note": ("design", "solution", "analysis", "technical", "communicat", "document", "engineering"),
    "sirishydro_ask": ("standard", "regulat", "legal", "code", "technical"),
    "engineering_standards_search": ("standard", "regulat", "legal", "code"),
}


def category_for(title: str) -> str:
    if _RISK_RE.search(title):
        return "Risk management"
    if _BUSINESS_RE.search(title):
        return "Business and management"
    return "Area of practice"


def _hours(event: dict[str, Any]) -> float:
    try:
        start, end = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
    except (KeyError, TypeError, ValueError):
        return 1.0
    hours = (end - start).total_seconds() / 3600
    return round(min(max(hours, 0.25), 8.0) * 4) / 4  # quarter hours, a working day at most


def _day_window(siris: "SirisAI", now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(clock.get_zone(siris.settings.timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def drafts(siris: "SirisAI") -> dict[str, list[dict[str, Any]]]:
    return {"cpd": list(siris.state.get(CPD_KEY) or []), "evidence": list(siris.state.get(EVIDENCE_KEY) or [])}


def add_cpd(siris: "SirisAI", title: str, hours: float, date: str, category: str | None = None, provider: str = "", notes: str = "") -> dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex[:10],
        "date": date,
        "title": title.strip()[:200],
        "hours": round(float(hours), 2),
        "category": category or category_for(title),
        "provider": provider[:120],
        "notes": notes[:500],
        "added": datetime.now(UTC).isoformat(),
    }
    siris.state.set(CPD_KEY, [*drafts(siris)["cpd"], entry][-500:])
    _to_brain(siris, f"CPD {date} {entry['title']}", f"{entry['hours']} h of CPD ({entry['category']}).\n\n{notes}".strip(), ["cpd"])
    return entry


def _to_brain(siris: "SirisAI", title: str, content: str, tags: list[str]) -> None:
    if siris.second_brain is None:
        return
    try:
        siris.second_brain.learn([{"title": title, "content": content, "tags": ["career", *tags], "source": "sirisai"}])
    except Exception:  # noqa: BLE001 - the draft is kept in SirisAI either way
        pass


def cpd_csv(siris: "SirisAI") -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "Activity", "Provider", "Hours", "Category", "Notes"])
    for entry in drafts(siris)["cpd"]:
        writer.writerow([entry["date"], entry["title"], entry["provider"], entry["hours"], entry["category"], entry["notes"]])
    return out.getvalue()


def _open_elements(siris: "SirisAI") -> list[dict[str, str]]:
    client = getattr(siris, "_sirisos_client", None)
    if client is None:
        return []
    try:
        overview = client.career().get("overview") or {}
    except Exception:  # noqa: BLE001 - suggestions are a bonus
        return []
    return [{"id": str(g.get("id")), "title": str(g.get("title"))} for g in (overview.get("competencies") or {}).get("gaps", [])]


def _suggest_elements(elements: list[dict[str, str]], tools: set[str]) -> list[str]:
    words = {w for t in tools for w in _ELEMENT_WORDS.get(t, ())}
    return [f"{e['id']}. {e['title']}" for e in elements if any(w in e["title"].lower() for w in words)][:3]


def capture(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    start, end = _day_window(siris, now)
    day = start.astimezone(clock.get_zone(siris.settings.timezone)).date().isoformat()
    out: dict[str, Any] = {"date": day, "cpd": [], "evidence": None}

    calendar = siris.tools.get("calendar_upcoming_events")
    events = []
    if calendar is not None:
        try:
            events = calendar._client.events_between(start, end)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - no calendar today is not an error
            events = []
    logged = {(e["date"], e["title"].lower()) for e in drafts(siris)["cpd"]}
    for event in events:
        title = str(event.get("summary") or "").strip()
        if not title or not CPD_RE.search(title) or (day, title.lower()) in logged:
            continue
        hours, category = _hours(event), category_for(title)
        siris.attention.raise_item(
            f"career:cpd:{day}:{title.lower()[:120]}",
            "suggestion",
            f"Log as CPD? {title} ({hours:g} h)",
            source=SOURCE,
            severity="info",
            body=f"On your calendar today. I'd log it as {hours:g} h of {category}. It goes on your CPD list to copy into Engineers Australia.",
            actions=[action("add_cpd", "Add to CPD", "primary"), action("dismiss", "Not CPD")],
            payload={"title": title, "hours": hours, "category": category, "date": day, "provider": event.get("location") or ""},
            reopen=False,
        )
        out["cpd"].append({"title": title, "hours": hours, "category": category})

    work = [e for e in siris.audit.since(start, limit=5000) if e["tool_name"] in ENGINEERING_TOOLS and not e["error"] and not e["confirmation_required"] and datetime.fromisoformat(e["created_at"]).replace(tzinfo=UTC) < end]
    if work:
        tools = {e["tool_name"] for e in work} | ({"sirishydro_ask"} if any((e["arguments"] or {}).get("standards_question") for e in work) else set())
        calcs = [str((e["arguments"] or {}).get("title") or (e["arguments"] or {}).get("calculator")) for e in work if e["tool_name"] in ("engineering_calculate", "engineering_calc_note")]
        questions = [str((e["arguments"] or {}).get("question") or (e["arguments"] or {}).get("query")) for e in work if e["tool_name"] in ("sirishydro_ask", "engineering_standards_search")]
        questions += [str(e["arguments"]["standards_question"]) for e in work if e["tool_name"] == "engineering_calc_note" and (e["arguments"] or {}).get("standards_question")]
        parts = []
        if calcs:
            parts.append(f"ran {len(calcs)} calculation{'s' if len(calcs) != 1 else ''} ({', '.join(dict.fromkeys(calcs))[:200]})")
        if questions:
            parts.append(f"checked the standards on {', '.join(dict.fromkeys(questions))[:200]}")
        summary = "Today you " + " and ".join(parts) + "."
        suggested = _suggest_elements(_open_elements(siris), tools)
        siris.attention.raise_item(
            f"career:evidence:{day}",
            "suggestion",
            "Chartership evidence from today?",
            source=SOURCE,
            severity="info",
            body=summary + (f" It could count towards: {'; '.join(suggested)}." if suggested else "") + " Keep it as a draft for your competency evidence.",
            actions=[action("keep_evidence", "Keep as evidence", "primary"), action("dismiss", "No thanks")],
            payload={"date": day, "summary": summary, "elements": suggested},
            reopen=False,
        )
        out["evidence"] = {"summary": summary, "elements": suggested}
    return out


def _add_cpd(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    entry = add_cpd(siris, payload["title"], payload["hours"], payload["date"], payload.get("category"), payload.get("provider") or "")
    return entry, f"Added {entry['hours']:g} h to the CPD list"


def _keep_evidence(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    entry = {"id": uuid.uuid4().hex[:10], "date": payload["date"], "summary": payload["summary"], "elements": payload.get("elements") or [], "added": datetime.now(UTC).isoformat()}
    siris.state.set(EVIDENCE_KEY, [*drafts(siris)["evidence"], entry][-500:])
    _to_brain(siris, f"Chartership evidence {entry['date']}", entry["summary"] + ("\n\nElements: " + "; ".join(entry["elements"]) if entry["elements"] else ""), ["chartership"])
    return entry, "Kept as chartership evidence"


register_action(SOURCE, "add_cpd", _add_cpd)
register_action(SOURCE, "keep_evidence", _keep_evidence)
