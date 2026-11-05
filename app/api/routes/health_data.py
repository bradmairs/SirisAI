"""POST /siris/health/metrics -- health data from a phone. Built for an
Apple Shortcuts "Get Contents of URL" step after "Find Health Samples":

    POST /siris/health/metrics
    Authorization: Bearer <key>        X-Siris-User: brad
    {"date": "2026-10-01", "metrics": {"sleep_hours": 7.2, "steps": 8123, "resting_heart_rate": 57}}

A flat {"steps": 8123, ...} body works too; `date` defaults to today in
SIRISAI_TIMEZONE. Unknown metric names are reported back, not stored.

Health Auto Export's own JSON ({"data": {"metrics": [...], "workouts":
[...]}}) works too, with any number of days in one post
(app/health/auto_export.py), so Apple Health can arrive on its own; so does
SirisAI's own {"metrics": {day: {...}}, "sleep": ..., "workouts": ...}.
Everything it sends is kept (every metric, sleep times, workouts).
`X-Health-Source` names where it came from (health_auto_export, shortcut,
sirisos...), for SIRISAI_HEALTH_SOURCE_PRIORITY.

    POST   /siris/health/apple-export     export.zip from the Health app (multipart "file"): years of history
    GET    /siris/health/export.csv?start=&end=
    GET    /siris/health/report?days=90   a printable summary for a doctor (print to PDF from the browser)
    DELETE /siris/health/data?start=&end=&source=
    GET    /siris/health/notes?days=30
    POST   /siris/health/meal             a photo of a meal (multipart "file"): logged with a rough estimate
"""

import asyncio
import shutil
import tempfile
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.core import clock
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import get_current_user_id, resolve_user_id
from app.core import guest
from app.health import apple_export, auto_export, battery, overview, report
from app.health.service import normalize, summary

router = APIRouter(prefix="/siris/health", tags=["health"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


@router.post("/metrics")
async def post_metrics(request: Request, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Send JSON.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Send a JSON object.")
    source = (request.headers.get("x-health-source") or "").strip().lower()[:40]
    if auto_export.looks_like(body) or auto_export.looks_native(body):
        parsed = auto_export.parse(body)
        source = source or ("health_auto_export" if auto_export.looks_like(body) else "sirisos")
        if not (parsed["metrics"] or parsed["workouts"]):
            raise HTTPException(status_code=422, detail=f"No recognised metrics (got: {', '.join(parsed['ignored']) or 'nothing'}).")
        if request.query_params.get("dry_run") in ("1", "true"):
            # 4.17: what an export would give, without keeping it (SirisOS's "Test the sync").
            return {"dry_run": True, "format": "health_auto_export" if auto_export.looks_like(body) else "sirisai", "meta": parsed.get("meta"),
                    "days": parsed["metrics"], "sleep": parsed["sleep"], "workouts": {d: len(w) for d, w in parsed["workouts"].items()},
                    "ignored": parsed["ignored"]}
        from app.health.service import merge_hour_sums

        parsed = merge_hour_sums(siris.state, parsed, get_current_user_id())
        result = siris.health.store_parsed(parsed, source)
        from app.health import gym_effort

        gym_effort.keep_series(siris, parsed.get("workout_series") or [])
        battery.keep_intraday(siris, parsed.get("intraday") or {})
        meta = parsed.get("meta")
        overview.note_import(siris, source, result, meta)
        if meta and meta.get("kind") == "metrics" and meta.get("workouts_sent"):  # one automation sending both
            overview.note_import(siris, source, result, {"kind": "workouts", "days_covered": meta.get("workout_days"), "workouts_sent": meta["workouts_sent"]})
        return {"user_id": get_current_user_id(), "format": "health_auto_export" if auto_export.looks_like(body) else "sirisai",
                "source": source, "days": parsed["metrics"], "workouts": result["workouts"], "ignored": parsed["ignored"]}
    raw_date = body.get("date")
    raw_metrics = body.get("metrics") if isinstance(body.get("metrics"), dict) else {k: v for k, v in body.items() if k != "date"}
    day = str(raw_date)[:10] if raw_date else clock.now_local(siris.settings.timezone).date().isoformat()
    try:
        date_type.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.") from None
    metrics, ignored = normalize(raw_metrics)
    if not metrics:
        raise HTTPException(status_code=422, detail=f"No recognised metrics (got: {', '.join(ignored) or 'nothing'}).")
    siris.health.record(metrics, day, source=source or "shortcut")
    overview.note_import(siris, source or "shortcut", {"days": 1, "readings": len(metrics)})
    return {"user_id": get_current_user_id(), "date": day, "stored": metrics, "ignored": ignored}


@router.post("/gym")
async def post_gym(request: Request, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """SirisGym's training summary from SirisOS (4.16), for the gym_progress tool."""
    from app.health import gym

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Send JSON.") from None
    if not isinstance(body, dict) or len(str(body)) > 200_000:
        raise HTTPException(status_code=422, detail="Send SirisGym's summary as a JSON object (under 200 KB).")
    gym.keep(siris, body)
    return {"kept": True, "lifts": len(body.get("lifts") or {})}


@router.post("/gym/session")
async def post_gym_session(request: Request, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """A SirisGym session with each set's time (4.28), for linking to the Watch workout and the effort analysis."""
    from app.health import gym_effort

    body = await request.json()
    if not isinstance(body, dict) or not body.get("id") or len(str(body)) > 300_000:
        raise HTTPException(status_code=422, detail="Send the session as a JSON object with an id.")
    gym_effort.keep_session(siris, body)
    return {"kept": True}


@router.get("/gym/coach")
def get_gym_coach(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """This week's coaching (4.29): training, sleep, recovery and protein, into at most two changes."""
    from app.health import gym_coach

    return gym_coach.weekly(siris)


@router.get("/gym/session/{session_id}/analysis")
def get_gym_analysis(session_id: str, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    from app.health import gym_effort

    return gym_effort.analyse(siris, session_id)


@router.get("/sources")
def get_sources(day: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"), siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Every source's value for each metric on a day, and which one counts (4.17)."""
    return siris.health.sources(day)


@router.get("/summary")
def get_summary(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    return summary(siris.health, clock.now_local(siris.settings.timezone).date())


@router.post("/apple-export")
async def apple_health_export(file: UploadFile = File(...), years: int = Query(5, ge=1, le=20), siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Backfill from the Health app's own export. Read as a stream; only days
    a higher-priority source hasn't already given are used for the totals."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "export.zip"
        with path.open("wb") as out:
            await asyncio.to_thread(shutil.copyfileobj, file.file, out, 1 << 20)
        try:
            parsed = await asyncio.to_thread(apple_export.parse_file, path, apple_export.default_since(years))
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # a truncated or odd file
            raise HTTPException(status_code=422, detail=f"Couldn't read that export: {type(exc).__name__}") from None
    result = await asyncio.to_thread(siris.health.store_parsed, parsed, "apple_export")
    overview.note_import(siris, "apple_export", result)
    first = min(parsed["metrics"]) if parsed["metrics"] else None
    return {"source": "apple_export", "from": first, **result}


def _range(siris: SirisAI, start: str | None, end: str | None, days: int = 3650) -> tuple[str, str]:
    today = clock.now_local(siris.settings.timezone).date()
    s, e = start or (today - timedelta(days=days)).isoformat(), end or today.isoformat()
    try:
        date_type.fromisoformat(s), date_type.fromisoformat(e)
    except ValueError:
        raise HTTPException(status_code=422, detail="start and end must be YYYY-MM-DD.") from None
    return s, e


def _not_guest(siris: SirisAI) -> None:
    if guest.is_on(siris):
        raise HTTPException(status_code=403, detail="Health data isn't available in guest mode.")


@router.get("/export.csv")
def export_csv(start: str | None = None, end: str | None = None, siris: SirisAI = Depends(get_siris)) -> PlainTextResponse:
    _not_guest(siris)
    s, e = _range(siris, start, end)
    return PlainTextResponse(siris.health.export_csv(s, e), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="health-{s}-to-{e}.csv"'})


@router.get("/report", response_class=HTMLResponse)
def doctor_report(days: int = Query(90, ge=7, le=730), siris: SirisAI = Depends(get_siris)) -> HTMLResponse:
    _not_guest(siris)
    return HTMLResponse(report.html(siris, days))


@router.delete("/data")
def delete_data(start: str, end: str, source: str | None = None, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _not_guest(siris)
    s, e = _range(siris, start, end)
    result = siris.health.delete(s, e, source or None)
    siris.audit.record("health_delete", {"start": s, "end": e, "source": source}, result=result, trigger="the user deleted health data")
    return {"start": s, "end": e, "source": source, **result}


@router.get("/notes")
def get_notes(days: int = Query(30, ge=1, le=365), siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    _not_guest(siris)
    today = clock.now_local(siris.settings.timezone).date()
    return {"notes": siris.health.notes((today - timedelta(days=days)).isoformat())}


MEAL_PROMPT = (
    "This is a photo of a meal. Estimate it roughly. Reply with exactly one line of JSON and nothing else: "
    '{"description": "what it is, short", "kcal": number, "protein_g": number}'
)


@router.post("/meal")
async def meal_photo(file: UploadFile = File(...), siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """A rough calorie and protein estimate from a photo, by the vision model
    (SIRISAI_VISION_MODEL, local). Logged as an estimate, never as fact."""
    import json as _json
    import re

    from app.plugins.builtin.health_data import log_meal

    _not_guest(siris)
    if siris.vision is None:
        raise HTTPException(status_code=404, detail="Image understanding isn't set up (SIRISAI_VISION_MODEL).")
    image = await file.read()
    if not image or len(image) > 15 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="Send one photo, under 15 MB.")
    answer = await asyncio.to_thread(siris.vision.describe, [image], MEAL_PROMPT)
    found = re.search(r"\{.*\}", answer or "", re.S)
    try:
        guess = _json.loads(found.group(0)) if found else {}
        kcal, protein = float(guess.get("kcal") or 0), float(guess.get("protein_g") or 0) or None
    except (ValueError, TypeError):
        kcal, protein, guess = 0.0, None, {}
    if kcal <= 0:
        raise HTTPException(status_code=422, detail="Couldn't make an estimate from that photo.")
    day = clock.now_local(siris.settings.timezone).date().isoformat()
    return await asyncio.to_thread(log_meal, siris, str(guess.get("description") or "a meal"), kcal, protein, day)
