"""Known fixes, applied (4.30.0).

Some breakages have one fix that always works, and this week each waited on a person anyway:

- **a companion container that stopped** (the Obsidian relay stops whenever the Obsidian container is recreated):
  restart it, check the service answers again;
- **a service that moved** on this same server (Hermes, after SirisAI's Docker network moved): switch to the address
  where it answers -- only an address on this server: the service's own container, or the server's LAN address;
- **an Ollama model partly in system RAM** (llama3.1:8b loaded 31 of 33 layers on 13 Nov, with the GPU half
  empty): unload it and load it again, and check it's all on the GPU.

Each fix is tried at most a few times (FIX_BUDGET a day per thing, the GPU reload once every GPU_EVERY_HOURS), then
it's left to Brad with what was tried. Every fix goes in the week's report and as a quiet note in the feed, so
nothing changes unseen. SIRISAI_AUTO_FIX_KNOWN=false goes back to asking first.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

import httpx

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "known_fixes"        # {"<what>": ["<iso time>", ...]}
FIX_BUDGET = 3                   # tries per thing per day
GPU_EVERY_HOURS = 6
ON_GPU = 0.98                    # a model this much in VRAM counts as all on the GPU
SOURCE = "known_fixes"


def _tries(siris: "SirisAI", what: str, window: timedelta, now: datetime) -> int:
    since = (now - window).isoformat()
    return sum(1 for t in (siris.state.get(STATE_KEY) or {}).get(what, []) if t >= since)


def _record(siris: "SirisAI", what: str, now: datetime) -> None:
    doc = dict(siris.state.get(STATE_KEY) or {})
    keep = (now - timedelta(days=2)).isoformat()
    doc[what] = [t for t in doc.get(what, []) if t >= keep] + [now.isoformat()]
    siris.state.set(STATE_KEY, doc)


def allowed(siris: "SirisAI", what: str, now: datetime | None = None, window: timedelta = timedelta(days=1), budget: int = FIX_BUDGET) -> bool:
    return siris.settings.auto_fix_known and _tries(siris, what, window, now or datetime.now(UTC)) < budget


def _tell(siris: "SirisAI", key: str, title: str, body: str, fixed: bool) -> None:
    from app.upkeep import week_review
    from app.upkeep.alerts import raise_alert

    week_review.log(siris, "fixed" if fixed else "unfixed", title)
    raise_alert(siris, f"{SOURCE}:{key}", title, body, severity="info" if fixed else "attention", push=not fixed)


def _wait(check: Callable[[], bool], seconds: float = 15.0) -> bool:
    end = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(1.5)


def restart_companion(siris: "SirisAI", service: str, container: str, answers: Callable[[], bool], now: datetime | None = None) -> bool:
    """Restart a stopped companion container and check the service answers. False when not allowed or not fixed."""
    now = now or datetime.now(UTC)
    what = f"restart:{container}"
    if not allowed(siris, what, now):
        return False
    _record(siris, what, now)
    try:
        siris.run_tool("docker_restart_container", {"container": container}, confirmed=True, trigger="self-healing: known fix")
    except Exception as exc:  # noqa: BLE001
        _tell(siris, what, f"{service} isn't answering, and restarting {container} failed",
              f"{container} was stopped. Restarting it failed: {exc}", fixed=False)
        return False
    ok = _wait(answers)
    _tell(siris, what, f"{service} answers again: {container} was stopped and has been restarted" if ok
          else f"{service} still isn't answering after restarting {container}",
          f"{container} was stopped, so {service} wasn't answering. It was restarted at {now.strftime('%H:%M')} UTC"
          + (", and " + service + " answers again." if ok else f", but {service} still doesn't answer. Check {container}'s logs."), fixed=ok)
    return ok


def ollama_models(base_url: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    c = client or httpx.Client(timeout=5.0)
    r = c.get(f"{base_url.rstrip('/')}/api/ps")
    r.raise_for_status()
    return list(r.json().get("models") or [])


def _spilled(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in models if (m.get("size") or 0) and (m.get("size_vram") or 0) < (m["size"] * ON_GPU)]


def gpu_fit(siris: "SirisAI", now: datetime | None = None, client: httpx.Client | None = None) -> list[str]:
    """Reload any Ollama model that's partly in system RAM. Returns the models reloaded."""
    now = now or datetime.now(UTC)
    if (siris.settings.llm_provider or "") != "ollama" or not siris.settings.ollama_base_url:
        return []
    base = siris.settings.ollama_base_url.rstrip("/")
    c = client or httpx.Client(timeout=120.0)
    try:
        spilled = _spilled(ollama_models(base, c))
    except Exception:  # noqa: BLE001 - Ollama down is the breakage watch's to say
        return []
    done = []
    for m in spilled:
        name = m.get("name") or m.get("model") or ""
        what = f"gpu:{name}"
        ram = round((m["size"] - (m.get("size_vram") or 0)) / 1e9, 1)
        if not allowed(siris, what, now, window=timedelta(hours=GPU_EVERY_HOURS), budget=1):
            continue
        _record(siris, what, now)
        try:
            c.post(f"{base}/api/generate", json={"model": name, "keep_alive": 0}).raise_for_status()
            c.post(f"{base}/api/generate", json={"model": name, "keep_alive": siris.settings.ollama_keep_alive or "30m"}).raise_for_status()
            after = next((x for x in ollama_models(base, c) if (x.get("name") or x.get("model")) == name), None)
        except Exception as exc:  # noqa: BLE001
            _tell(siris, what, f"{name} is partly in system RAM, and reloading it failed", f"{ram} GB of it was outside the GPU. {exc}", fixed=False)
            continue
        ok = after is not None and not _spilled([after])
        _tell(siris, what, f"{name} was {ram} GB in system RAM: reloaded, all on the GPU now" if ok
              else f"{name} is still partly in system RAM after reloading it",
              f"Part of {name} had loaded into system RAM (slower, and it takes the server's memory). It was unloaded and "
              "loaded again" + (", and is all on the GPU now." if ok else ", but it still doesn't fit: something else is "
              "using the GPU (nvidia-smi shows what), or a second model is loaded beside it."), fixed=ok)
        done.append(name)
    return done
