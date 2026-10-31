"""Self-update with rollback.

SirisAI runs in a container, so it can't rebuild itself. The work is split:

- **SirisAI** (here) checks GitHub once a day (SIRISAI_UPDATE_CHECK_HOUR_LOCAL,
  9am) for a release newer than its own version. With SIRISAI_UPDATE_MODE
  `notify` (the default) it puts "SirisAI 1.3 is out" in the attention feed
  with the release notes and an "Update now" button; with `auto` it asks
  for the update straight away, so updates land in that hour; `off` doesn't
  check. Asking means writing `request.json` into SIRISAI_UPDATE_DIR (the
  data folder's `update/`), with the version wanted and today's tool
  selection accuracy as the bar the new version has to clear.
- **The updater** (`deploy/updater.sh`, run every minute by cron on the
  server; `make auto-update` installs it) picks the request up: it refuses
  if either checkout has local edits, fast-forwards SirisAI to the release
  tag, pulls SirisOS, rebuilds and restarts, then checks both apps answer,
  SirisAI reports the new version, and the selection eval hasn't got
  worse. If any check fails it puts both checkouts back where they were,
  rebuilds, and records why in `status.json`.
- **SirisAI again**, once it's back up, reads `status.json` and says how it
  went: "Updated to 1.3", or "Update to 1.3 rolled back: ..." pushed, with a
  GitHub issue proposed. A request nobody picks up for 15 minutes means the
  updater isn't installed, and it says that too.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app import __version__
from app.hub.attention import action, register_action
from app.upkeep import alerts, backups

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

SOURCE = "updates"
SEEN_KEY = "update_status_seen"
UNCLAIMED_AFTER = timedelta(minutes=15)


def update_dir(siris: "SirisAI") -> Path:
    return Path(siris.settings.update_dir) if siris.settings.update_dir else backups.data_dir(siris.settings) / "update"


def as_decimal(version: str) -> Decimal | None:
    try:
        return Decimal(version.strip().lstrip("v"))
    except (InvalidOperation, AttributeError):
        return None


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = as_decimal(candidate), as_decimal(current)
    return a is not None and b is not None and a > b


def latest_release(siris: "SirisAI") -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json"}
    if siris.settings.github_token:
        headers["Authorization"] = f"Bearer {siris.settings.github_token}"
    with httpx.Client(base_url=siris.settings.github_api_url.rstrip("/"), headers=headers, timeout=20) as http:
        response = http.get(f"/repos/{siris.settings.update_repo}/releases/latest")
        response.raise_for_status()
        body = response.json()
    return {"version": str(body.get("tag_name", "")).lstrip("v"), "notes": body.get("body") or "", "url": body.get("html_url")}


def _baseline(siris: "SirisAI") -> float | None:
    try:
        from app.evals import tool_choice

        return tool_choice.selection_report(siris)["accuracy"]
    except Exception:  # noqa: BLE001 - no bar is better than no update
        logger.exception("Selection eval for the update baseline failed")
        return None


def request(siris: "SirisAI", version: str, reason: str = "asked") -> dict[str, Any]:
    if not is_newer(version):
        raise ValueError(f"{version} isn't newer than the running {__version__}")
    folder = update_dir(siris)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "request.json"
    if path.exists() or (folder / "request.processing.json").exists():
        return {"requested": False, "detail": "An update is already waiting for the updater", **json.loads((path if path.exists() else folder / "request.processing.json").read_text())}
    body = {
        "version": version,
        "from_version": __version__,
        "baseline_selection_accuracy": _baseline(siris),
        "reason": reason,
        "requested_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(body, indent=2))
    return {"requested": True, **body}


def check(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    mode = siris.settings.update_mode
    if mode == "off":
        return {"skipped": "SIRISAI_UPDATE_MODE=off"}
    release = latest_release(siris)
    if not is_newer(release["version"]):
        return {"latest": release["version"], "running": __version__, "update": False}
    if mode == "auto":
        return {"latest": release["version"], "running": __version__, "update": True, **request(siris, release["version"], "automatic")}
    siris.attention.raise_item(
        f"update:{release['version']}",
        "suggestion",
        f"SirisAI {release['version']} is out",
        source=SOURCE,
        severity="info",
        body=(release["notes"][:1500] or "A new version is available.") + f"\n\nYou're on {__version__}. Updating rebuilds and restarts SirisAI and SirisOS, and rolls back on its own if they don't come back healthy.",
        link=release.get("url"),
        actions=[action("apply", "Update now", "primary", confirm="Rebuild and restart SirisAI and SirisOS now?"), action("not_now", "Not now")],
        payload={"version": release["version"]},
        reopen=False,
    )
    return {"latest": release["version"], "running": __version__, "update": True, "asked": True}


def status(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any]:
    folder = update_dir(siris)
    out: dict[str, Any] = {"running": __version__, "mode": siris.settings.update_mode, "dir": str(folder)}
    for name, key in (("request.json", "pending"), ("request.processing.json", "processing"), ("status.json", "last")):
        path = folder / name
        if path.exists():
            try:
                out[key] = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                out[key] = {"unreadable": True}
    beat = folder / "heartbeat"
    try:
        seen = datetime.fromisoformat(beat.read_text().strip().replace("Z", "+00:00")) if beat.exists() else None
    except (OSError, ValueError):
        seen = None
    # The updater writes a heartbeat every minute (4.03); none for 5 minutes and it isn't running.
    out["updater_seen"] = seen.isoformat() if seen else None
    out["updater_running"] = bool(seen and (now or datetime.now(UTC)) - seen < timedelta(minutes=5))
    pending = out.get("pending")
    if pending and pending.get("requested_at"):
        waited = (now or datetime.now(UTC)) - datetime.fromisoformat(pending["requested_at"])
        out["updater_missing"] = waited > UNCLAIMED_AFTER
    return out


def report(siris: "SirisAI", now: datetime | None = None) -> dict[str, Any] | None:
    """Say how the last update went, once; and whether the updater is missing."""
    current = status(siris, now)
    if current.get("updater_missing"):
        alerts.raise_alert(
            siris,
            "upkeep:updater-missing",
            "The updater isn't running",
            f"SirisAI asked for {current['pending'].get('version')} but nothing has picked it up. On the server, run `make auto-update` in SirisAI/deploy once to install the updater.",
        )
    last = current.get("last")
    if not last or not last.get("finished_at") or last.get("state") == "running":
        return None
    if siris.state.get(SEEN_KEY) == last["finished_at"]:
        return None
    siris.state.set(SEEN_KEY, last["finished_at"])
    siris.attention.resolve_key("upkeep:updater-missing", "The updater picked the request up")
    state, version = last.get("state"), last.get("version")
    if state == "updated":
        alerts.raise_alert(siris, f"upkeep:update:{version}", f"Updated to {version}", f"SirisAI {last.get('from_version')} → {version}, checked healthy." + (f" {last['detail']}" if last.get("detail") else ""), severity="info")
        siris.outcomes.record("updates", "kept", key=f"update:{version}", source="updater", ref=f"update:{last['finished_at']}")
    elif state == "skipped":
        alerts.raise_alert(siris, f"upkeep:update:{version}", f"Update to {version} skipped", last.get("detail") or "The updater didn't apply it.")
    else:
        detail = last.get("detail") or "no detail"
        tail = (last.get("log_tail") or "")[-1500:]
        alerts.raise_alert(
            siris,
            f"upkeep:update:{version}",
            f"Update to {version} rolled back" if state == "rolled_back" else f"Update to {version} failed",
            f"{detail}. I'm still on {__version__}." + (f"\n\nLast log lines:\n{tail}" if tail else ""),
            severity="urgent" if state != "rolled_back" else "attention",
            push=True,
            issue=True,
        )
        siris.outcomes.record("updates", "undone", key=f"update:{version}", source="updater", detail={"why": detail}, ref=f"update:{last['finished_at']}")
    return last


def _apply(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = request(siris, payload["version"], "approved in the attention feed")
    return result, f"Update to {payload['version']} requested"


def _not_now(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    return None, "Not now"


register_action(SOURCE, "apply", _apply)
register_action(SOURCE, "not_now", _not_now)
