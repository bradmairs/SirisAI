"""Container image updates on the Docker host (SIRISAI_DOCKER_BASE_URL).

- **Checking** (daily at SIRISAI_CONTAINER_UPDATES_HOUR_LOCAL, 5am): for each
  running container, asks the registry (through the Docker daemon, so its
  logins apply) for the current digest of the image it runs, and compares it
  with the digests the local image was pulled as. Containers whose image is
  pinned to a digest, was built locally, is labelled `siris.update=false` (or
  Watchtower's `com.centurylinklabs.watchtower.enable=false`), is in
  SIRISAI_CONTAINER_UPDATE_EXCLUDE, or is SirisAI itself, are left alone.
  Updates go to the attention feed with **Update all**.
- **Updating** (`docker_update_containers`, or the feed button; asks first):
  the same way Watchtower does it. Pull the new image, stop the old
  container and rename it aside, create the new one from the old one's
  settings (minus whatever came from the old image, so the new image's
  defaults apply), and start it. If it doesn't come up healthy within
  SIRISAI_CONTAINER_UPDATE_HEALTH_SECONDS (a failing health check, or it
  stops), the new one is removed and the old one renamed back and started.
  Only once the new one is healthy is the old container removed (its image
  stays, for a manual roll-back).
- **From the feed** (3.52) the update runs in the background and the button
  answers at once: 22 pulls and health checks take minutes, far longer than
  a phone waits for a reply. The front doors (the Caddy containers in front
  of SirisAI and SirisOS) go last, since restarting them drops connections.
  When it's done, a notice says what was updated; anything rolled back or
  failed gets its own alert, as before.
- **Progress** (3.72): while it runs, one feed item says where it's got to
  ("Updating 7 of 22: sonarr"), so the Inbox shows it live; a container that
  fails or is rolled back has the last lines of its log in its alert.
- **Watching afterwards** (3.72): for SIRISAI_CONTAINER_UPDATE_WATCH_MINUTES
  (10; 0 turns it off) after the run, each updated container is checked
  every half minute. One that stops, restarts or turns unhealthy is put back
  on the image it had before (kept for exactly this) with the same settings,
  and an alert says so with its last log lines.
- **Shared networks** (4.31.2): a container that others join with
  `network_mode: service:x` / `container:x` (gluetun with wg-easy or a
  torrent client behind it) can't be swapped on its own: Docker pins the
  others to the old container's id, so they'd be left on a network that's
  gone. The ones sharing it are recreated on the new one (same settings)
  before the old one is removed, and rolled back with it if any of them
  doesn't come up.
- **Does it answer** (4.32): the TCP ports a container publishes are tried
  just before it's stopped; any that answered then has to answer again
  (within the health wait) or the update is rolled back. A container can be
  "healthy" while its web page is gone; this catches that.
- **History and roll-back later** (4.32): every update, roll-back and failure
  is kept (the last 200, CONTAINER_HISTORY_KEY), with the image it was on
  before. `roll_back_to(name)` puts a container back on that image whenever
  you like, the same way an update swaps it (shared networks included).
"""

import contextvars
import hashlib
import logging
import socket
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.docker.client import DockerClient
from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

STATE_KEY = "container_updates"
OPT_OUT_LABELS = {"siris.update": "false", "com.centurylinklabs.watchtower.enable": "false"}
ASIDE = "-siris-old"


def _client(siris: "SirisAI") -> DockerClient:
    client = getattr(siris, "_docker_client", None)
    if client is None:
        raise RuntimeError("Docker isn't configured (SIRISAI_DOCKER_BASE_URL).")
    return client


def _excluded(siris: "SirisAI") -> set[str]:
    from app.homelab import tracking

    untracked = {e["name"] for e in tracking.untracked(siris).values() if e.get("kind") == "container"}  # 5.2
    return {n.strip() for n in siris.settings.container_update_exclude.split(",") if n.strip()} | untracked


def _is_self(container_id: str) -> bool:
    # Inside a container, the hostname is the container's short id.
    host = socket.gethostname()
    return len(host) >= 12 and container_id.startswith(host)


def check(siris: "SirisAI", now: datetime | None = None, raise_feed: bool = True) -> dict[str, Any]:
    """{"updates": [{"container", "image", ...}], "up_to_date": [...], "skipped": {...}}."""
    client = _client(siris)
    excluded = _excluded(siris)
    updates: list[dict[str, Any]] = []
    up_to_date: list[str] = []
    skipped: dict[str, str] = {}
    remote_cache: dict[str, str | None] = {}
    for summary in client.list_containers(all_containers=False):
        name = (summary.get("names") or [summary["id"]])[0]
        try:
            info = client.inspect_container(summary["id"])
            reason = _skip_reason(info, name, excluded)
            if reason:
                skipped[name] = reason
                continue
            ref = info["Config"]["Image"]
            local = client.inspect_image(info["Image"])
            pulled_as = {d.split("@", 1)[1] for d in local.get("RepoDigests") or [] if "@" in d}
            if not pulled_as:
                skipped[name] = "built locally"
                continue
            if ref not in remote_cache:
                remote_cache[ref] = client.remote_digest(ref)
            remote = remote_cache[ref]
        except Exception as exc:  # noqa: BLE001 - one registry being down shouldn't stop the rest
            skipped[name] = f"couldn't check: {str(exc)[:150]}"
            continue
        if remote is None:
            skipped[name] = "the registry gave no digest"
        elif remote in pulled_as:
            up_to_date.append(name)
        else:
            update = {"container": name, "image": ref, "current": sorted(pulled_as)[0][:19], "available": remote[:19]}
            from app.homelab import release_notes

            notes = release_notes.summarise(siris, (local.get("Config") or {}).get("Labels") or {})  # 6.58
            if notes:
                update["notes"] = notes
            updates.append(update)
    report = {"updates": updates, "up_to_date": sorted(up_to_date), "skipped": skipped, "checked_at": (now or datetime.now(UTC)).isoformat()}
    siris.state.set(STATE_KEY, report)
    if raise_feed:
        _sync_feed(siris, updates)
    return report


def _skip_reason(info: dict[str, Any], name: str, excluded: set[str]) -> str | None:
    labels = info.get("Config", {}).get("Labels") or {}
    from app.homelab import tracking

    if name in excluded:
        return "excluded (SIRISAI_CONTAINER_UPDATE_EXCLUDE)"
    if tracking.norm(name) in {tracking.norm(x) for x in excluded}:
        return "not tracked (you asked SirisAI to stop tracking it)"
    if any(str(labels.get(k, "")).lower() == v for k, v in OPT_OUT_LABELS.items()):
        return "opted out by label"
    if _is_self(info.get("Id", "")):
        return "SirisAI itself (it updates itself, see SIRISAI_UPDATE_MODE)"
    if "@sha256:" in info.get("Config", {}).get("Image", ""):
        return "pinned to a digest"
    if name.endswith(ASIDE):
        return "the old copy from an update"
    if labels.get(REF_LABEL):
        return HELD
    return None


def _sync_feed(siris: "SirisAI", updates: list[dict[str, Any]]) -> None:
    from app.homelab import release_notes

    wanted = []
    if updates:
        names = [u["container"] for u in updates]
        # A new set of updates is a new item, so dismissing one batch doesn't hide the next.
        digest = hashlib.sha1("|".join(f"{u['container']}@{u['available']}" for u in updates).encode()).hexdigest()[:10]
        listed = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
        wanted.append(
            {
                "key": f"containers:updates:{digest}",
                "kind": "suggestion",
                "title": f"{len(names)} container update{'s' if len(names) != 1 else ''}: {listed}"
                + (f" ({breaking} with breaking changes)" if (breaking := sum(1 for u in updates if (u.get("notes") or {}).get("breaking"))) else ""),
                "body": "\n".join(f"{u['container']}: {u['image']}" for u in updates[:20])
                + (("\nWhat's new:\n" + "\n".join(n for n in notes if n)) if any(notes := [release_notes.line(u["container"], u.get("notes")) for u in updates[:20]]) else "")
                + "\nEach is rolled back automatically if it doesn't come up healthy.",
                "severity": "info",
                "actions": [action("update", "Update all", "primary", confirm=f"Update {len(names)} container{'s' if len(names) != 1 else ''}?"), action("dismiss", "Not now")],
                "payload": {"containers": names},
            }
        )
    siris.attention.sync("containers", wanted, outcome="Up to date")


# -- updating -----------------------------------------------------------------------------

# Settings that came from the image rather than the container's own config:
# copying them would pin the old image's values onto the new one.
_IMAGE_DEFAULTS = ("Cmd", "Entrypoint", "WorkingDir", "User", "StopSignal", "Healthcheck")


def new_config(container: dict[str, Any], old_image: dict[str, Any], image_ref: str) -> dict[str, Any]:
    """The create body for the replacement: the container's own settings,
    with the old image's defaults taken out."""
    config = dict(container["Config"])
    image_config = old_image.get("Config") or {}
    config["Image"] = image_ref
    for key in _IMAGE_DEFAULTS:
        if key in config and config[key] == image_config.get(key):
            config.pop(key)
    image_env = set(image_config.get("Env") or [])
    config["Env"] = [e for e in config.get("Env") or [] if e not in image_env]
    image_labels = image_config.get("Labels") or {}
    config["Labels"] = {k: v for k, v in (config.get("Labels") or {}).items() if image_labels.get(k) != v}
    for key in ("ExposedPorts", "Volumes"):
        own = {k: v for k, v in (config.get(key) or {}).items() if k not in (image_config.get(key) or {})}
        config[key] = own or None
    if config.get("Hostname") == container["Id"][:12]:
        config.pop("Hostname")  # Docker's default; the new container gets its own
    if str((container.get("HostConfig") or {}).get("NetworkMode", "")).startswith("container:"):
        # Another container's network: Docker refuses a hostname or ports of its own.
        for key in ("Hostname", "Domainname"):
            config.pop(key, None)
        config["ExposedPorts"] = None
        config["HostConfig"] = container.get("HostConfig") or {}
        config["NetworkingConfig"] = {}
        config["_extra_networks"] = {}
        return config
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    endpoints = {net: _endpoint(container, ep) for net, ep in networks.items()}
    first = next(iter(endpoints), None)
    config["HostConfig"] = container.get("HostConfig") or {}
    config["NetworkingConfig"] = {"EndpointsConfig": {first: endpoints[first]}} if first else {}
    config["_extra_networks"] = {k: v for k, v in endpoints.items() if k != first}
    return config


def _endpoint(container: dict[str, Any], endpoint: dict[str, Any]) -> dict[str, Any]:
    aliases = [a for a in endpoint.get("Aliases") or [] if a != container["Id"][:12]]
    out: dict[str, Any] = {"Aliases": aliases or None}
    if endpoint.get("IPAMConfig"):
        out["IPAMConfig"] = endpoint["IPAMConfig"]
    if endpoint.get("Links"):
        out["Links"] = endpoint["Links"]
    return out


def _healthy(client: DockerClient, container_id: str, wait_seconds: float, poll: float) -> tuple[bool, str]:
    deadline = time.monotonic() + wait_seconds
    settle = min(10.0, wait_seconds)
    started = time.monotonic()
    while True:
        state = client.inspect_container(container_id).get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        if not state.get("Running"):
            return False, f"it stopped (exit code {state.get('ExitCode')})"
        if health == "unhealthy":
            return False, "its health check failed"
        if health == "healthy" or (health is None and time.monotonic() - started >= settle):
            return True, health or "running"
        if time.monotonic() >= deadline:
            return (False, "it didn't pass its health check in time") if health else (True, "running")
        time.sleep(poll)


def _sharing(client: DockerClient, container: dict[str, Any]) -> list[str]:
    """The containers that run in this one's network namespace."""
    cid, name = container["Id"], (container.get("Name") or "").lstrip("/")
    wanted = {f"container:{cid}", f"container:{name}", f"container:{cid[:12]}"}
    return [
        (c.get("names") or [c["id"]])[0]
        for c in client.list_containers(all_containers=True)
        if c.get("network_mode") in wanted and not (c.get("names") or [""])[0].endswith(ASIDE)
    ]


def _move(client: DockerClient, name: str, onto: str) -> tuple[dict[str, Any], str]:
    """Recreate `name` (same image and settings) in `onto`'s network namespace.
    The old one is stopped and renamed aside, not removed. Returns (old, new id)."""
    old = client.inspect_container(name)
    config = new_config(old, client.inspect_image(old["Image"]), old["Config"]["Image"])
    config.pop("_extra_networks", None)
    config["HostConfig"] = {**config["HostConfig"], "NetworkMode": f"container:{onto}"}
    client.stop(old["Id"])
    client.rename(old["Id"], name + ASIDE)
    try:
        new_id = client.create(name, config)
    except Exception:
        client.rename(old["Id"], name)
        raise
    return old, new_id


HISTORY_KEY = "container_update_history"
REF_LABEL = "siris.ref"  # the tag a container put back on an older image id came from
HELD = "kept on an earlier version (you put it back): update it from SirisOS to carry on"


def _ref(container: dict[str, Any]) -> str:
    """The image tag a container follows."""
    return ((container.get("Config") or {}).get("Labels") or {}).get(REF_LABEL) or container["Config"]["Image"]


def _record(siris: "SirisAI", result: dict[str, Any], before: str | None = None, after: str | None = None, trigger: str = "update") -> None:
    """Keep what happened, newest first (4.32)."""
    entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "container": result["container"], "status": result["status"],
             "trigger": trigger, "before": before, "after": after, "why": result.get("why"), "also": result.get("also_recreated") or []}
    try:
        history = siris.state.get(HISTORY_KEY) or []
        siris.state.set(HISTORY_KEY, [entry, *history][:200])
    except Exception:  # noqa: BLE001 - the history is a nicety; never fail an update over it
        logger.exception("Recording the container update failed")


def history(siris: "SirisAI", limit: int = 50) -> list[dict[str, Any]]:
    return (siris.state.get(HISTORY_KEY) or [])[:limit]


def _probe_host(siris: "SirisAI") -> str | None:
    if siris.settings.container_probe_host:
        return siris.settings.container_probe_host
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
    except OSError:
        pass
    return None


def _tcp_ports(container: dict[str, Any]) -> list[int]:
    bindings = (container.get("HostConfig") or {}).get("PortBindings") or {}
    out = set()
    for spec, binds in bindings.items():
        if spec.endswith("/tcp"):
            for b in binds or []:
                if str(b.get("HostPort") or "").isdigit():
                    out.add(int(b["HostPort"]))
    return sorted(out)


def _answers(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _answering(siris: "SirisAI", container: dict[str, Any]) -> tuple[str | None, list[int]]:
    """The published TCP ports that answer right now (before an update)."""
    host = _probe_host(siris)
    if not host:
        return None, []
    return host, [p for p in _tcp_ports(container) if _answers(host, p)]


def _answer_again(host: str | None, ports: list[int], wait_seconds: float, poll: float) -> list[int]:
    """The ports still not answering after the wait: [] is good."""
    if not host or not ports:
        return []
    deadline = time.monotonic() + max(wait_seconds, 5.0)
    left = list(ports)
    while left:
        left = [p for p in left if not _answers(host, p)]
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(poll)
    return left


def update(siris: "SirisAI", name: str, poll: float = 2.0, release: bool = False) -> dict[str, Any]:
    """`release`: update one you'd put back on an earlier version (it's held there otherwise)."""
    client = _client(siris)
    old = client.inspect_container(name)
    reason = _skip_reason(old, name, _excluded(siris))
    if reason and not (release and reason == HELD):
        return {"container": name, "status": "skipped", "why": reason}
    ref = _ref(old)
    client.pull(ref)
    fresh = client.inspect_image(ref)
    if fresh.get("Id") == old["Image"]:
        return {"container": name, "status": "up to date"}
    result = _swap(siris, client, old, ref, poll)
    _record(siris, result, before=old["Image"], after=fresh.get("Id"))
    return result


def roll_back_to(siris: "SirisAI", name: str, image_id: str | None = None, poll: float = 2.0) -> dict[str, Any]:
    """Put a container back on the image it had before its last update (or `image_id`), any time (4.32)."""
    client = _client(siris)
    old = client.inspect_container(name)
    if not image_id:
        last = next((h for h in history(siris, 200) if h["container"] == name and h["status"] == "updated" and h.get("before")), None)
        if not last:
            return {"container": name, "status": "failed", "why": "No earlier version is recorded for it."}
        image_id = last["before"]
    if image_id == old["Image"]:
        return {"container": name, "status": "up to date", "why": "It's already on that version."}
    try:
        client.inspect_image(image_id)
    except Exception:  # noqa: BLE001
        return {"container": name, "status": "failed", "why": "That version's image isn't on the server any more."}
    result = _swap(siris, client, old, image_id, poll, keep_ref=_ref(old))
    if result["status"] == "updated":
        result["status"] = "put back"
    _record(siris, result, before=old["Image"], after=image_id, trigger="roll back")
    return result


def _swap(siris: "SirisAI", client: DockerClient, old: dict[str, Any], ref: str, poll: float, keep_ref: str | None = None) -> dict[str, Any]:
    """Replace `old` with a container on image `ref`, same settings; roll back if it doesn't come up."""
    name = old["Name"].lstrip("/")
    old_image = client.inspect_image(old["Image"])
    config = new_config(old, old_image, ref)
    config["Labels"] = {k: v for k, v in (config.get("Labels") or {}).items() if k != REF_LABEL}
    if keep_ref:
        # Put back on an image id: the tag now points at the newer image, so remember it, and hold the container
        # there (the morning's check leaves it alone) until you update it again.
        config["Labels"][REF_LABEL] = keep_ref
    extra = config.pop("_extra_networks")
    was_running = bool((old.get("State") or {}).get("Running"))
    sharing = _sharing(client, old)
    moved: list[tuple[dict[str, Any], str]] = []
    host, answering = _answering(siris, old) if was_running else (None, [])
    client.stop(old["Id"])
    client.rename(old["Id"], name + ASIDE)
    new_id = None
    try:
        new_id = client.create(name, config)
        for network, endpoint in extra.items():
            client.connect(network, new_id, endpoint)
        client.start(new_id)
        ok, how = _healthy(client, new_id, siris.settings.container_update_health_seconds, poll)
        if not ok:
            raise RuntimeError(how)
        for other in sharing:
            moved.append(_move(client, other, new_id))
            client.start(moved[-1][1])
            ok, other_how = _healthy(client, moved[-1][1], siris.settings.container_update_health_seconds, poll)
            if not ok:
                raise RuntimeError(f"{other}, which shares its network, didn't come up on the new one: {other_how}")
        silent = _answer_again(host, answering, siris.settings.container_update_health_seconds, poll)
        if silent:
            raise RuntimeError(f"port {', '.join(map(str, silent))} answered before the update and doesn't now")
    except Exception as exc:  # noqa: BLE001 - every failure rolls back
        logs = ""
        for other_old, other_new in reversed(moved):
            try:
                client.remove(other_new)
            except Exception:  # noqa: BLE001
                logger.exception("Removing the new %s failed", other_old["Name"])
            client.rename(other_old["Id"], other_old["Name"].lstrip("/"))
        if new_id:
            logs = _tail(client, new_id)
            try:
                client.remove(new_id)
            except Exception:  # noqa: BLE001
                logger.exception("Removing the failed new %s failed", name)
        client.rename(old["Id"], name)
        if was_running:
            client.start(old["Id"])
            _restart_sharing(client, sharing)
        return {"container": name, "status": "rolled back", "why": str(exc)[:300], "logs": logs}
    _previous[name] = {"config": {**new_config(old, old_image, old["Image"]), "_extra_networks": extra}, "image": old["Image"]}
    for other_old, _ in moved:
        client.remove(other_old["Id"])
    client.remove(old["Id"])
    result = {"container": name, "status": "updated", "image": keep_ref or ref, "health": how}
    if answering:
        result["answering"] = answering
    if sharing:
        result["also_recreated"] = sharing
    return result


def _restart_sharing(client: DockerClient, names: list[str]) -> None:
    """The old network namespace went with the stop; joining containers need a restart to get it back."""
    for other in names:
        try:
            client.stop(other)
            client.start(other)
        except Exception:  # noqa: BLE001 - best effort; the alert says to check
            logger.exception("Restarting %s after a roll-back failed", other)


# What each container ran before its update, for a roll-back while it's watched.
_previous: dict[str, dict[str, Any]] = {}
# Set by the background run so update_many can say how far it's got.
_progress: contextvars.ContextVar[Any] = contextvars.ContextVar("container_update_progress", default=None)


def _tail(client: DockerClient, container: str, lines: int = 15) -> str:
    try:
        return client.get_container_logs(container, tail=lines)[-1500:]
    except Exception:  # noqa: BLE001 - logs are a nicety
        return ""


def update_many(siris: "SirisAI", names: list[str]) -> list[dict[str, Any]]:
    results = []
    report = _progress.get()
    for i, name in enumerate(names):
        if report:
            report(i, len(names), name)
        try:
            results.append(update(siris, name))
        except Exception as exc:  # noqa: BLE001 - say which one, carry on with the rest
            results.append({"container": name, "status": "failed", "why": str(exc)[:300]})
            _record(siris, results[-1])
    for r in results:
        if r["status"] in ("rolled back", "failed"):
            back = r["status"] == "rolled back"
            siris.attention.raise_item(
                f"containers:failed:{r['container']}",
                "alert",
                f"{r['container']}'s update was rolled back" if back else f"Updating {r['container']} failed",
                source="containers-failed",
                severity="attention",
                body=(f"The new version didn't come up: {r['why']}. The old one is running again."
                      if back else f"{r['why']}. Check that {r['container']} is running.")
                + (f"\n\nIts last log lines:\n{r['logs']}" if r.get("logs") else ""),
            )
    return results


_running = threading.Lock()
_worker: threading.Thread | None = None  # the last background run (tests join it)


def front_doors_last(names: list[str]) -> list[str]:
    """Reverse proxies last: restarting one drops the connection you asked through."""
    def door(n: str) -> bool:
        low = n.lower()
        return "caddy" in low or "https" in low or "proxy" in low or "traefik" in low or "nginx" in low
    return [n for n in names if not door(n)] + [n for n in names if door(n)]


def _finish(siris: "SirisAI", names: list[str], trigger: str, watch_poll: float = 30.0) -> None:
    run = int(time.time())
    progress_key = f"containers:progress:{run}"

    def report(i: int, n: int, name: str) -> None:
        siris.attention.raise_item(progress_key, "notice", f"Updating {i + 1} of {n}: {name}", source="containers-progress",
                                   severity="info", body="Pulling the new image and checking it comes up healthy. You can leave this; a notice follows.")

    token = _progress.set(report)
    updated: list[str] = []
    try:
        result = siris.run_tool("docker_update_containers", {"containers": ",".join(names)}, confirmed=True, trigger=trigger)
        updated = [r["container"] for r in result if r["status"] == "updated"]
        done = [r["container"] for r in result if r["status"] == "updated"]
        same = [r["container"] for r in result if r["status"] in ("up to date", "skipped")]
        back = [r["container"] for r in result if r["status"] in ("rolled back", "failed")]
        title = f"Updated {len(done)} of {len(names)} containers" + (f"; {len(back)} rolled back" if back else "")
        body = "\n".join(x for x in [f"Updated: {', '.join(done)}" if done else "", f"Already current: {', '.join(same)}" if same else "",
                                      f"Rolled back or failed (see their own alerts): {', '.join(back)}" if back else ""] if x)
        minutes = siris.settings.container_update_watch_minutes
        if done and minutes > 0:
            body += f"\nWatching them for {minutes:g} minutes; any that falls over goes back to the version it had."
        siris.attention.raise_item(f"containers:done:{run}", "notice", title, source="containers-done",
                                   severity="attention" if back else "info", body=body or None)
    except Exception as exc:  # noqa: BLE001 - say so rather than fail silently in a thread
        logger.exception("Background container update failed")
        siris.attention.raise_item(f"containers:done:{run}", "alert", "The container update stopped", source="containers-done",
                                   severity="attention", body=f"{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        _progress.reset(token)
        siris.attention.resolve_key(progress_key, "Finished")
        try:
            if updated and siris.settings.container_update_watch_minutes > 0:
                watch(siris, updated, siris.settings.container_update_watch_minutes * 60, watch_poll)
        except Exception:  # noqa: BLE001
            logger.exception("Watching the updated containers failed")
        finally:
            _running.release()


def _trouble(info: dict[str, Any], restarts: int) -> str | None:
    state = info.get("State") or {}
    if state.get("Restarting") or int(info.get("RestartCount") or 0) > restarts:
        return "it kept restarting"
    if not state.get("Running"):
        return f"it stopped (exit code {state.get('ExitCode')})"
    if (state.get("Health") or {}).get("Status") == "unhealthy":
        return "its health check started failing"
    return None


def watch(siris: "SirisAI", names: list[str], seconds: float, poll: float = 30.0) -> list[dict[str, Any]]:
    """Check freshly updated containers for a while; put any that falls over
    back on its previous image. Returns what was rolled back."""
    client = _client(siris)
    restarts = {}
    for n in names:
        try:
            restarts[n] = int(client.inspect_container(n).get("RestartCount") or 0)
        except Exception:  # noqa: BLE001
            restarts[n] = 0
    watching, rolled = set(names), []
    deadline = time.monotonic() + seconds
    while watching:
        for name in sorted(watching):
            try:
                why = _trouble(client.inspect_container(name), restarts[name])
            except Exception as exc:  # noqa: BLE001 - gone altogether
                why = f"it disappeared ({str(exc)[:80]})"
            if why and name in _previous:
                rolled.append(_roll_back(siris, client, name, why))
                watching.discard(name)
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
    for name in names:
        _previous.pop(name, None)
    return rolled


def _roll_back(siris: "SirisAI", client: DockerClient, name: str, why: str) -> dict[str, Any]:
    prev = _previous[name]
    logs = _tail(client, name)
    config = dict(prev["config"])
    extra = config.pop("_extra_networks", {}) or {}
    try:
        try:
            client.stop(name)
        except Exception:  # noqa: BLE001 - already stopped
            pass
        sharing = _sharing(client, client.inspect_container(name))
        client.remove(name)
        new_id = client.create(name, config)
        for network, endpoint in extra.items():
            client.connect(network, new_id, endpoint)
        client.start(new_id)
        for other in sharing:
            other_old, other_new = _move(client, other, new_id)
            client.start(other_new)
            client.remove(other_old["Id"])
        title, outcome = f"{name} was rolled back after its update", "rolled back"
        body = f"The new version ran at first, then {why}. It's back on the image it had before ({prev['image'][:19]})."
    except Exception as exc:  # noqa: BLE001
        title, outcome = f"{name} fell over after its update, and rolling it back failed", "failed"
        body = f"After the update {why}. Putting the old version back failed: {str(exc)[:200]}. Check {name} by hand."
    siris.audit.record("docker_update_rollback", {"container": name, "why": why}, result=outcome, trigger="the container update's watch")
    _record(siris, {"container": name, "status": outcome, "why": why}, before=None, after=prev["image"], trigger="watch")
    siris.attention.raise_item(f"containers:failed:{name}", "alert", title, source="containers-failed", severity="attention",
                               body=body + (f"\n\nIts last log lines:\n{logs}" if logs else ""))
    return {"container": name, "status": outcome, "why": why}


def _update_action(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    global _worker
    names = front_doors_last(list(payload["containers"]))
    if not _running.acquire(blocking=False):
        return {"started": 0}, "An update is already running"
    ctx = contextvars.copy_context()  # who asked (the user) carries into the thread
    _worker = threading.Thread(target=ctx.run, args=(_finish, siris, names, trigger), name="container-updates", daemon=True)
    _worker.start()
    return {"started": len(names), "order": names}, f"Updating {len(names)} in the background; a notice follows when it's done"


register_action("containers", "update", _update_action)


def run_one(siris: "SirisAI", name: str, action: str) -> dict[str, Any]:
    """From SirisOS (4.32): put a container back on its earlier version ("roll-back"), or update one ("update",
    which also releases one you'd put back). Runs in the background; the outcome lands in the history and the feed."""
    global _worker
    if action not in ("roll-back", "update"):
        raise ValueError(action)
    _client(siris).inspect_container(name)  # KeyError-ish failures surface now, not in the thread
    if not _running.acquire(blocking=False):
        return {"started": False, "why": "An update is already running"}
    ctx = contextvars.copy_context()

    def work() -> None:
        try:
            r = roll_back_to(siris, name) if action == "roll-back" else update(siris, name, release=True)
            ok = r["status"] in ("updated", "put back", "up to date")
            title = {"put back": f"{name} is back on its earlier version", "updated": f"Updated {name}",
                     "up to date": f"{name} is already current"}.get(r["status"], f"{name}: {r['status']}")
            siris.attention.raise_item(f"containers:one:{name}:{int(time.time())}", "notice" if ok else "alert", title, source="containers-done",
                                       severity="info" if ok else "attention", body=r.get("why"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Container %s %s failed", action, name)
            _record(siris, {"container": name, "status": "failed", "why": str(exc)[:300]}, trigger=action)
        finally:
            _running.release()

    _worker = threading.Thread(target=lambda: ctx.run(work), daemon=True, name=f"container-{action}")
    _worker.start()
    return {"started": True}


def overview(siris: "SirisAI") -> dict[str, Any]:
    """For SirisOS's Home Lab (4.32): the last check, what's waiting, held, and what happened lately."""
    report = siris.state.get(STATE_KEY) or {}
    held = {n: why for n, why in (report.get("skipped") or {}).items() if why == HELD}
    return {"checked_at": report.get("checked_at"), "updates": report.get("updates") or [], "up_to_date": len(report.get("up_to_date") or []),
            "held": sorted(held), "running": _running.locked(), "history": history(siris, 40)}
