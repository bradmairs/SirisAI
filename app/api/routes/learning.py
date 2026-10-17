"""The learning loop over HTTP (app/learning/): outcomes, reflections,
learned eval cases, and the once-a-day jobs."""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from app.core import daily_jobs
from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id
from app.learning import feedback
from app.learning.reflection import HISTORY_KEY, LEARNED_CASES_KEY, learned_cases, reflect

router = APIRouter(prefix="/siris/learning", tags=["learning"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)])


@router.get("")
def overview(days: float = 30, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """How things went per tool and suggestion source, and what SirisAI now
    asks before doing on its own."""
    return {
        "asking_first_now": feedback.demoted_keys(siris),
        "summary": siris.outcomes.summary(days=days),
        "learned_eval_cases": len(learned_cases(siris)),
    }


@router.get("/outcomes")
def outcomes(limit: int = 50, signal: str | None = None, siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    return siris.outcomes.recent(limit=min(limit, 500), signal=signal)


@router.post("/reflect")
async def reflect_now(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Run the nightly reflection now."""
    return await asyncio.to_thread(reflect, siris)


@router.get("/reflections")
def reflections(siris: SirisAI = Depends(get_siris)) -> list[dict[str, Any]]:
    return list(reversed(siris.state.get(HISTORY_KEY) or []))


@router.get("/eval-cases", response_model=None)
def eval_cases(format: str = "json", siris: SirisAI = Depends(get_siris)) -> Any:
    """Eval cases learned from corrections. ?format=jsonl gives lines ready to
    append to evals/tool_choice.jsonl."""
    cases = learned_cases(siris)
    if format == "jsonl":
        return PlainTextResponse("".join(json.dumps({"prompt": c["prompt"], "expect": c["expect"]}) + "\n" for c in cases))
    return cases


@router.delete("/eval-cases")
def clear_eval_cases(siris: SirisAI = Depends(get_siris)) -> dict[str, int]:
    """Forget learned cases (after committing them to the repo, say)."""
    count = len(learned_cases(siris))
    siris.state.delete(LEARNED_CASES_KEY)
    return {"cleared": count}


@router.get("/jobs")
def jobs(siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """The once-a-day jobs: when each last ran and what it reported."""
    runs = daily_jobs.last_runs(siris)
    results = siris.state.get(daily_jobs.RESULTS_KEY) or {}
    return {
        job.name: {"hour": job.hour(siris), "weekday": job.weekday(siris), "monthday": job.monthday, "last_run": runs.get(job.name), "last_result": results.get(job.name)}
        for job in daily_jobs.JOBS
    }


@router.post("/jobs/{name}/run")
async def run_job(name: str, siris: SirisAI = Depends(get_siris)) -> dict[str, Any]:
    """Run one of the once-a-day jobs now, whatever the hour."""
    job = next((j for j in daily_jobs.JOBS if j.name == name), None)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job '{name}'. Jobs: {', '.join(j.name for j in daily_jobs.JOBS)}")
    result = await asyncio.to_thread(daily_jobs.run_now, siris, job)
    return {"job": name, "result": result}
