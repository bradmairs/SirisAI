"""Model upgrades gated by evals: never switch to a model that's worse at
picking tools than the one in use.

A trial runs the tool-choice evals (evals/tool_choice.jsonl plus the cases
learned from corrections) against the current model and a candidate, on the
same Ollama server, one case at a time. When the candidate scores at least
as well, "Switch to qwen3:14b? 92% vs 88%" goes to the attention feed;
switching takes effect at once and survives restarts (kept in app_state),
and "Revert" goes back to SIRISAI_OLLAMA_MODEL. A candidate that scores
worse is reported and left alone.

Trials run on request (`POST /siris/upkeep/models/trial {"model": ...}`) and
weekly (SIRISAI_MODEL_TRIAL_DAY/_HOUR_LOCAL) for each model listed in
SIRISAI_CANDIDATE_MODELS that hasn't been tried in four weeks. Pull a model
into Ollama, list it, and SirisAI tries it out on its own.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

SOURCE = "models"
OVERRIDE_KEY = "llm_model_override"
TRIALS_KEY = "model_trials"
RETRY_AFTER = timedelta(days=28)


def current_model(siris: "SirisAI") -> str:
    return siris.state.get(OVERRIDE_KEY) or siris.settings.ollama_model


def build(siris: "SirisAI", model: str) -> Any:
    from app.llm.factory import build_llm_client
    from app.llm.metering import MeteredLLMClient

    return MeteredLLMClient(build_llm_client(siris.settings.llm_provider, siris.settings.ollama_base_url, model), siris.metrics)


def trial(siris: "SirisAI", model: str, now: datetime | None = None) -> dict[str, Any]:
    if siris.settings.llm_provider != "ollama":
        raise ValueError("Model trials need SIRISAI_LLM_PROVIDER=ollama")
    from app.evals import tool_choice

    now = now or datetime.now(UTC)
    in_use = current_model(siris)
    candidate_llm = build(siris, model)
    try:
        baseline = tool_choice.choice_report(siris)
        candidate = tool_choice.choice_report(siris, llm=candidate_llm)
    finally:
        try:
            import asyncio

            asyncio.run(candidate_llm.aclose())
        except Exception:  # noqa: BLE001 - closing a scratch client is best-effort
            pass
    better_or_equal = candidate["accuracy"] >= baseline["accuracy"] and candidate["cases"] > 0
    result = {
        "at": now.isoformat(),
        "model": model,
        "current": in_use,
        "current_accuracy": baseline["accuracy"],
        "candidate_accuracy": candidate["accuracy"],
        "cases": candidate["cases"],
        "candidate_failures": candidate["failures"][:10],
        "recommend_switch": better_or_equal and model != in_use,
    }
    trials = [*(siris.state.get(TRIALS_KEY) or []), result][-30:]
    siris.state.set(TRIALS_KEY, trials)
    if result["recommend_switch"]:
        siris.attention.raise_item(
            f"models:switch:{model}",
            "suggestion",
            f"Switch to {model}? {candidate['accuracy']}% vs {baseline['accuracy']}%",
            source=SOURCE,
            severity="info",
            body=f"{model} picked the right tool in {candidate['accuracy']}% of {candidate['cases']} eval cases; {in_use} managed {baseline['accuracy']}%. Switching takes effect straight away, and you can revert any time.",
            actions=[action("switch", f"Switch to {model}", "primary"), action("dismiss", "Keep " + in_use)],
            payload={"model": model},
        )
    else:
        siris.attention.raise_item(
            f"models:trial:{model}",
            "notice",
            f"Kept {in_use}: {model} scored {candidate['accuracy']}%",
            source=SOURCE,
            severity="info",
            body=f"{model} picked the right tool in {candidate['accuracy']}% of eval cases, against {baseline['accuracy']}% for {in_use}, so I'm staying put.",
        )
    return result


def switch(siris: "SirisAI", model: str | None) -> dict[str, Any]:
    """Use `model` from now on (None = back to SIRISAI_OLLAMA_MODEL)."""
    previous = current_model(siris)
    if model and model != siris.settings.ollama_model:
        siris.state.set(OVERRIDE_KEY, model)
    else:
        siris.state.delete(OVERRIDE_KEY)
    siris.llm = build(siris, current_model(siris))
    return {"previous": previous, "now": current_model(siris)}


def apply_override(siris: "SirisAI") -> None:
    """At startup: keep using a model switched to earlier."""
    try:
        override = siris.state.get(OVERRIDE_KEY)
    except Exception:  # noqa: BLE001 - no table yet (first boot, some tests)
        return
    if override and siris.settings.llm_provider == "ollama":
        siris.llm = build(siris, override)


def weekly(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    candidates = [m.strip() for m in siris.settings.candidate_models.split(",") if m.strip()]
    if not candidates or siris.settings.llm_provider != "ollama":
        return {"skipped": "no SIRISAI_CANDIDATE_MODELS"}
    now = now or datetime.now(UTC)
    tried = {t["model"]: datetime.fromisoformat(t["at"]) for t in siris.state.get(TRIALS_KEY) or []}
    results = []
    for model in candidates:
        if model == current_model(siris) or (model in tried and now - tried[model] < RETRY_AFTER):
            continue
        try:
            results.append(trial(siris, model, now))
        except Exception as exc:  # noqa: BLE001 - one bad candidate doesn't stop the rest
            logger.exception("Trial of %s failed", model)
            results.append({"model": model, "error": str(exc)[:200]})
    return {"trials": results}


def _switch_action(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    return switch(siris, payload["model"]), f"Switched to {payload['model']}"


register_action(SOURCE, "switch", _switch_action)
