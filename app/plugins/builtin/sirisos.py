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
        "GVW Timesheets, SirisDrone, Second Brain...): ok, degraded, down or not configured. Use for 'is APD PM "
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
