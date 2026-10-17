"""SirisOS tools (app/sirisos/client.py): the engineering library SirisOS
owns -- standards search, the deterministic civil/water calculators,
projects, SirisHydro -- and the state of every app it monitors. Registered
when SIRISAI_SIRISOS_URL is set. All READ: nothing here changes SirisOS.

Calculations always go through `engineering_calculate`, never the model's
own arithmetic: SirisOS runs the same calculator code its own screens use,
so an answer by voice matches the one on the Calculators screen."""

from typing import Any

from app.plugins.base import Tool, ToolPermission
from app.sirisos.client import SirisOSClient


class SirisOSAppsStatusTool(Tool):
    name = "sirisos_apps_status"
    description = (
        "The state of every app on the SirisOS home screen (APD PM, Engineering Reviewer, Engineering Archive, "
        "GVW Timesheets, SirisDrone, Siris...): ok, degraded, down or not configured. Use for 'is APD PM "
        "up', 'are my apps working', 'what's down on SirisOS'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        apps = [
            {"id": a.get("id"), "name": a.get("name"), "state": (a.get("status") or {}).get("state"), "detail": (a.get("status") or {}).get("detail") or ""}
            for a in self._client.apps()
        ]
        return {
            "apps": apps,
            "needs_a_look": [a["name"] for a in apps if a["state"] in ("down", "degraded")],
        }


class EngineeringStandardsSearchTool(Tool):
    name = "engineering_standards_search"
    description = (
        "Search Brad's private engineering standards library in SirisOS (Australian Standards, water authority "
        "and council guidelines, uploaded as PDFs) and return matching pages with citations. Use for 'what does "
        "AS3500 say about...', 'find the clause on minimum cover', 'which standard covers pit spacing'. Quote the "
        "citation with anything you take from a page."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to search for, e.g. 'minimum grade sewer'."},
            "authority": {"type": "string", "description": "Optional: only this authority, e.g. 'Standards Australia', 'Melbourne Water'."},
            "limit": {"type": "integer", "description": "How many pages, 1-12. Defaults to 6."},
        },
        "required": ["query"],
    }

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, query: str = "", authority: str | None = None, limit: int = 6, **kwargs: Any) -> Any:
        found = self._client.search_standards(query, authority=authority, limit=max(1, min(int(limit), 12)))
        hits = []
        for hit in found.get("hits") or []:
            doc = hit.get("document") or {}
            hits.append(
                {
                    "standard": " ".join(x for x in (doc.get("reference"), doc.get("title")) if x),
                    "authority": doc.get("authority"),
                    "edition": doc.get("edition"),
                    "page": hit.get("page"),
                    "citation": hit.get("citation"),
                    "excerpt": hit.get("snippet"),
                }
            )
        return {"query": found.get("query", query), "hits": hits}


class EngineeringCalculatorsTool(Tool):
    name = "engineering_calculators"
    description = (
        "List the civil/water engineering calculators in SirisOS (Manning pipe and channel flow, minimum grade, "
        "critical depth, Rational Method, detention storage, weir, orifice, Hazen-Williams, Darcy-Weisbach, minor "
        "loss, pump power, pipe buoyancy) with each one's id and inputs (SI units). Call before "
        "engineering_calculate when unsure of the id or input keys."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return [
            {
                "id": c["id"],
                "title": c["title"],
                "category": c.get("category"),
                "inputs": {f["key"]: f"{f['label']}" + (f" ({f['unit']})" if f.get("unit") else "") for f in c.get("fields") or []},
            }
            for c in self._client.calculators()
        ]


class EngineeringCalculateTool(Tool):
    name = "engineering_calculate"
    description = (
        "Run one of SirisOS's engineering calculators and return its results -- always use this instead of doing "
        "hydraulics arithmetic yourself. E.g. calculator 'fullPipe' with inputs {d: 0.45, n: 0.013, s: 0.005} "
        "(diameter m, Manning n, grade m/m); 'rationalMethod' with {c: 0.8, i: 100, a: 1.0} (C, mm/h, ha); "
        "'hazenWilliams' with {q: 0.05, d: 0.2, l: 100, c: 140}. engineering_calculators lists every id and input key."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "calculator": {"type": "string", "description": "Calculator id, e.g. 'fullPipe', 'rationalMethod'."},
            "inputs": {"type": "object", "description": "Input values by key, as numbers in SI units."},
        },
        "required": ["calculator", "inputs"],
    }

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, calculator: str = "", inputs: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        values: dict[str, float] = {}
        for key, value in (inputs or {}).items():
            try:
                values[key] = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"Input '{key}' must be a number, got {value!r}.") from None
        return self._client.calculate(calculator.strip(), values)


class EngineeringProjectsTool(Tool):
    name = "engineering_projects"
    description = "List Brad's engineering projects in SirisOS (name, kind, status, tags). Use for 'what projects do I have on', 'is Werribee active'."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"status": {"type": "string", "description": "Optional: only projects with this status, e.g. 'active'."}},
        "required": [],
    }

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, status: str | None = None, **kwargs: Any) -> Any:
        return [
            {k: p.get(k) for k in ("id", "name", "description", "kind", "status", "tags", "updated_at")}
            for p in self._client.projects()
            if not status or p.get("status") == status
        ]


class SirisHydroAskTool(Tool):
    name = "sirishydro_ask"
    description = (
        "Ask SirisHydro, SirisOS's civil/water engineering assistant, a question answered only from Brad's "
        "standards library, with numbered citations. Returns the answer (when the library holds enough evidence), "
        "the cited pages, and whether the evidence was sufficient. Prefer it over answering standards questions "
        "from memory; if it says the evidence is insufficient, say so rather than guessing."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "The engineering question, in full."}},
        "required": ["question"],
    }

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, question: str = "", **kwargs: Any) -> Any:
        found = self._client.sirishydro(question)
        return {
            "question": found.get("question", question),
            "sufficient_evidence": bool(found.get("sufficient_evidence")),
            "answer": found.get("synthesized_answer"),
            "guidance": found.get("guidance"),
            "evidence": [
                {"n": i, "citation": e.get("citation"), "excerpt": (e.get("excerpt") or "")[:500]}
                for i, e in enumerate(found.get("evidence") or [], start=1)
            ],
        }


def _career(client: SirisOSClient) -> tuple[dict[str, Any], dict[str, Any]]:
    found = client.career()
    return found.get("document") or {}, found.get("overview") or {}


class CareerCpdStatusTool(Tool):
    name = "career_cpd_status"
    description = (
        "Brad's CPD (continuing professional development) over the rolling 3 years Engineers Australia counts: "
        "total against the 150 hours, the minimums (50 area of practice, 10 risk management, 15 business and "
        "management), what's short, hours about to drop out of the window, and recent activities. Comes from "
        "his Engineers Australia CPD record as last imported into SirisOS. Use for 'how's my CPD', 'how many "
        "CPD hours do I need', 'am I on track for CPD'. CPD is logged in Engineers Australia, not here."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        doc, ov = _career(self._client)
        cpd = ov.get("cpd") or {}
        if not cpd.get("records"):
            return {"imported": False, "how": "No CPD imported yet. Export it from Engineers Australia (myPortal, "
                    "Record my CPD, export as CSV or Excel) and import it in SirisOS: Career, CPD."}
        window = cpd.get("window") or {}
        recent = [
            {"date": r.get("date"), "title": r.get("title"), "hours": r.get("hours"), "provider": r.get("provider") or None}
            for r in (doc.get("cpd") or {}).get("records", [])[:5]
        ]
        return {
            "imported": True,
            "window": f"{window.get('from')} to {window.get('to')}",
            "total_hours": cpd.get("total"),
            "required_hours": cpd.get("required"),
            "hours_short": cpd.get("short"),
            "requirement_met": cpd.get("met"),
            "minimums": [{"category": m["label"], "hours": m["hours"], "minimum": m["minimum"], "short": m["short"]} for m in cpd.get("minimums", [])],
            "hours_leaving_window_in_90_days": cpd.get("expiring_90_days"),
            "hours_by_year": cpd.get("by_year"),
            "recent_activities": recent,
            "guessed_categories": cpd.get("guessed"),
            "last_imported": (cpd.get("last_import") or {}).get("at"),
            "latest_activity": cpd.get("latest_activity"),
        }


class CareerChartershipStatusTool(Tool):
    name = "career_chartership_status"
    description = (
        "Where Brad is on the path to Chartered (CPEng) with Engineers Australia, the National Engineering "
        "Register and registration as a civil engineer in Victoria: progress on each pathway and its next step, "
        "which of the 16 Stage 2 competency elements still need evidence, his career goals, and the overall next "
        "steps. Use for 'what's my next chartership step', 'how close am I to CPEng', 'what do I need for "
        "registration', 'what are my career goals'."
    )
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: SirisOSClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        doc, ov = _career(self._client)
        comp = ov.get("competencies") or {}
        return {
            "next_steps": [f"{s['title']} ({s['detail']})" for s in ov.get("next_steps", [])],
            "pathways": [
                {"name": p["name"], "steps_done": p["done"], "steps": p["total"], "next_step": (p.get("next") or {}).get("title")}
                for p in ov.get("pathways", [])
            ],
            "competencies": {
                "elements_with_evidence": comp.get("evidenced"),
                "elements": comp.get("total"),
                "still_need_evidence": [f"{g['id']}. {g['title']}" for g in comp.get("gaps", [])],
            },
            "goals": [{"goal": g["title"], "by": g.get("target_date"), "next_step": g.get("next_step") or None} for g in ov.get("goals", [])],
            "evidence_items": len(doc.get("evidence") or []),
        }


class EngineeringCalcNoteTool(Tool):
    name = "engineering_calc_note"
    description = (
        "Write up an engineering calculation as a calc note in one go: runs the SirisOS calculator, looks up what "
        "Brad's standards library says (SirisHydro, with citations) when a standards question is given, ties it to "
        "a SirisOS project, and saves the note to Siris. Use for 'size the stormwater pipe for Werribee "
        "and write it up', 'do a calc note for the culvert'. Returns the note as markdown either way."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What the note is, e.g. 'Werribee DN450 outlet capacity'."},
            "calculator": {"type": "string", "description": "Calculator id, e.g. 'fullPipe' (engineering_calculators lists them)."},
            "inputs": {"type": "object", "description": "Input values by key, as numbers in SI units."},
            "project": {"type": "string", "description": "Optional: the SirisOS project's name or id."},
            "standards_question": {"type": "string", "description": "Optional: what to check in the standards, e.g. 'minimum cover for a DN450 stormwater pipe'."},
            "notes": {"type": "string", "description": "Optional: assumptions or context to record."},
        },
        "required": ["title", "calculator", "inputs"],
    }

    def __init__(self, client: SirisOSClient, brain: Any = None) -> None:
        self._client = client
        self._brain = brain

    def run(
        self,
        title: str = "",
        calculator: str = "",
        inputs: dict[str, Any] | None = None,
        project: str | None = None,
        standards_question: str | None = None,
        notes: str | None = None,
        **kwargs: Any,
    ) -> Any:
        calc = EngineeringCalculateTool(self._client).run(calculator=calculator, inputs=inputs)
        spec = next((c for c in self._client.calculators() if c["id"] == calculator.strip()), {})
        fields = {f["key"]: f for f in spec.get("fields") or []}
        match = None
        if project:
            wanted = project.strip().lower()
            match = next((p for p in self._client.projects() if p.get("id") == project or str(p.get("name", "")).lower() == wanted), None)
            match = match or next((p for p in self._client.projects() if wanted in str(p.get("name", "")).lower()), None)
        standards = SirisHydroAskTool(self._client).run(question=standards_question) if standards_question else None

        lines = [f"# {title.strip() or calc.get('title') or calculator}", ""]
        if project:
            lines.append(f"**Project:** {match['name']} ({match.get('status')})" if match else f"**Project:** {project} (not found in SirisOS)")
        lines += [f"**Calculator:** {calc.get('title') or calculator} (SirisOS `{calculator}`)", "", "## Inputs", "", "| Input | Value | Unit |", "| --- | --- | --- |"]
        for key, value in (calc.get("inputs") or inputs or {}).items():
            field = fields.get(key, {})
            lines.append(f"| {field.get('label', key)} | {value} | {field.get('unit') or ''} |")
        lines += ["", "## Results", ""]
        lines += [f"- **{r.get('label')}:** {r.get('value')}" for r in calc.get("results") or []] or ["- (no results returned)"]
        if standards is not None:
            lines += ["", "## Standards", "", f"_Question: {standards_question}_", ""]
            if standards["sufficient_evidence"] and standards.get("answer"):
                lines.append(standards["answer"])
            else:
                lines.append("The standards library doesn't hold enough to answer this. Check it by hand.")
            lines += [""] + [f"{e['n']}. {e['citation']}" for e in standards["evidence"]]
        if notes:
            lines += ["", "## Notes", "", notes.strip()]
        lines += ["", "---", "_Calculated by SirisOS's calculators and written up by SirisAI. Check it before relying on it._"]
        markdown = "\n".join(lines)

        saved = None
        if self._brain is not None:
            tags = ["calc-note", "engineering"] + ([str(match["name"]).lower().replace(" ", "-")] if match else [])
            result = self._brain.learn([{"title": title.strip() or calculator, "content": markdown, "tags": tags, "source": "sirisos", "links": [match["name"]] if match else []}])
            saved = result[0] if result else None
        return {"markdown": markdown, "results": calc.get("results"), "project": match, "standards": standards, "saved_to_brain": saved}
