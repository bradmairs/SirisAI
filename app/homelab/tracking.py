"""Things you've asked SirisAI to stop tracking (5.51).

A container removed or stopped on purpose (Open WebUI, say) kept coming back as
"Container 'open-webui' is exited" in every health check, pushed as urgent, and
self-healing would alert about it too. Stop tracking it, and nothing about it is
checked, restarted, offered for update or pushed, until you track it again.

Kept in state `untracked`: {"container:open-webui": {"kind", "name", "since", "why"}}.
Names match without case, a leading "/" or the difference between "-", "_" and " ",
so "Open WebUI" finds "open-webui".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

KEY = "untracked"
KINDS = ("container",)


def norm(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower().lstrip("/"))


def untracked(siris: "SirisAI") -> dict[str, dict[str, Any]]:
    state = getattr(siris, "state", None)  # no store (a bare test double): nothing is untracked
    return dict(state.get(KEY) or {}) if state is not None else {}


def is_tracked(siris: "SirisAI", kind: str, name: str) -> bool:
    return f"{kind}:{norm(name)}" not in untracked(siris)


def tracked_containers(siris: "SirisAI", containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The containers still tracked: one is untracked when any of its names is."""
    off = {k.split(":", 1)[1] for k in untracked(siris) if k.startswith("container:")}
    return [c for c in containers if not any(norm(n) in off for n in c.get("names") or [])]


def stop(siris: "SirisAI", kind: str, name: str, why: str = "") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"Can't stop tracking a {kind}: only {', '.join(KINDS)}.")
    if not norm(name):
        raise ValueError("Say what to stop tracking.")
    shown = str(name).strip().lstrip("/")
    client = getattr(siris, "_docker_client", None)
    if kind == "container" and client is not None:  # "Open WebUI" -> the container's own name, "open-webui"
        try:
            shown = next((n for c in client.list_containers(all_containers=True) for n in c.get("names") or [] if norm(n) == norm(name)), shown)
        except Exception:  # noqa: BLE001 - Docker unreachable: keep what was said
            pass
    kept = untracked(siris)
    entry = {"kind": kind, "name": shown[:80], "since": datetime.now(UTC).isoformat(timespec="seconds"),
             "why": str(why or "").strip()[:200]}
    kept[f"{kind}:{norm(name)}"] = entry
    siris.state.set(KEY, kept)
    return {"tracked": False, **entry}


def resume(siris: "SirisAI", kind: str, name: str) -> dict[str, Any]:
    kept = untracked(siris)
    gone = kept.pop(f"{kind}:{norm(name)}", None)
    siris.state.set(KEY, kept)
    return {"tracked": True, "kind": kind, "name": (gone or {}).get("name") or name, "was_untracked": gone is not None}


def overview(siris: "SirisAI") -> dict[str, Any]:
    """Every container Docker knows (tracked or not) and everything untracked, for SirisOS's switches."""
    containers: list[dict[str, Any]] | None = None
    client = getattr(siris, "_docker_client", None)
    if client is not None:
        try:
            listed = client.list_containers(all_containers=True)
            containers = [{"name": (c.get("names") or [c.get("id")])[0], "state": c.get("state") or "", "status": c.get("status") or "",
                           "tracked": is_tracked(siris, "container", (c.get("names") or [c.get("id")])[0])}
                          for c in listed]
            containers.sort(key=lambda c: (not c["tracked"], c["state"] == "running", c["name"].lower()))  # stopped first, untracked last
        except Exception:  # noqa: BLE001 - the list of untracked things still shows
            containers = None
    return {"containers": containers, "untracked": sorted(untracked(siris).values(), key=lambda e: e["name"].lower())}
