"""SirisAI files its own GitHub issues for problems it can't fix itself.

When self-healing gives up on a container, a tool keeps failing until the
circuit breaker pauses it, an update has to be rolled back, or the restore
drill fails, SirisAI writes up what happened -- the error, the recent
failures from its audit log, its version -- and either asks first ("File a
GitHub issue?" in the attention feed, the default) or files it straight
away (SIRISAI_GITHUB_ISSUES_MODE=auto). A fix can then land as a normal
+0.01 release.

Needs SIRISAI_GITHUB_ISSUES_REPO (owner/name) and SIRISAI_GITHUB_TOKEN (a
fine-grained token with Issues: read and write on that repo only); without
both, nothing is proposed. Issues carry the `filed-by-sirisai` label. If an
open one with the same title exists, SirisAI comments on it instead (at
most once a day per problem). Argument values whose names look like
secrets (token, password, key...) are blanked before anything is sent.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from app import __version__
from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

SOURCE = "issues"
LABEL = "filed-by-sirisai"
STATE_KEY = "github_issues_filed"
_SECRET_NAME = re.compile(r"token|passw|secret|api[_-]?key|auth|credential|cookie|session|private", re.I)
_SECRET_VALUE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")


def configured(siris: "SirisAI") -> bool:
    return bool(siris.settings.github_issues_repo and siris.settings.github_token)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[redacted]" if _SECRET_NAME.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(lambda m: (m.group(1) or "") + "[redacted]", value)
    return value


def _http(siris: "SirisAI") -> httpx.Client:
    return httpx.Client(
        base_url=siris.settings.github_api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {siris.settings.github_token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        timeout=20,
    )


def compose_body(siris: "SirisAI", summary: str) -> str:
    since = datetime.now(UTC) - timedelta(hours=24)
    failures = [e for e in siris.audit.since(since, limit=2000) if e["error"]][-10:]
    lines = [
        summary,
        "",
        f"- SirisAI version: {__version__}",
        f"- LLM provider: {siris.settings.llm_provider}",
        f"- Seen: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if failures:
        lines += ["", "### Recent failures (last 24 h)", ""]
        for e in failures:
            args = json.dumps(redact(e["arguments"]), default=str)[:300]
            lines.append(f"- `{e['tool_name']}` {args}: {redact(str(e['error']))[:300]} ({e['created_at'][:16]}, trigger: {e.get('trigger') or 'none'})")
    lines += ["", "---", "_Filed by SirisAI itself (app/upkeep/issues.py)._"]
    return redact("\n".join(lines))


def file(siris: "SirisAI", title: str, summary: str, fingerprint: str) -> dict[str, Any]:
    """File (or comment on the open issue with the same title)."""
    repo = siris.settings.github_issues_repo
    title = f"[SirisAI] {title}"[:250]
    body = compose_body(siris, summary)
    filed = dict(siris.state.get(STATE_KEY) or {})
    with _http(siris) as http:
        found = http.get(f"/repos/{repo}/issues", params={"labels": LABEL, "state": "open", "per_page": 100})
        found.raise_for_status()
        existing = next((i for i in found.json() if i.get("title") == title), None)
        if existing is not None:
            last = filed.get(fingerprint, {}).get("commented_at") or filed.get(fingerprint, {}).get("at")
            if last and datetime.now(UTC) - datetime.fromisoformat(last) < timedelta(days=1):
                return {"number": existing["number"], "url": existing.get("html_url"), "action": "skipped (commented today)"}
            http.post(f"/repos/{repo}/issues/{existing['number']}/comments", json={"body": "It happened again.\n\n" + body}).raise_for_status()
            filed[fingerprint] = {**filed.get(fingerprint, {}), "number": existing["number"], "url": existing.get("html_url"), "commented_at": datetime.now(UTC).isoformat()}
            siris.state.set(STATE_KEY, filed)
            return {"number": existing["number"], "url": existing.get("html_url"), "action": "commented"}
        created = http.post(f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": [LABEL]})
        created.raise_for_status()
        issue = created.json()
    filed[fingerprint] = {"number": issue["number"], "url": issue.get("html_url"), "at": datetime.now(UTC).isoformat()}
    siris.state.set(STATE_KEY, filed)
    return {"number": issue["number"], "url": issue.get("html_url"), "action": "filed"}


def propose(siris: "SirisAI", title: str, summary: str, fingerprint: str) -> dict[str, Any] | None:
    """Ask in the feed, or file straight away in auto mode. None when not set up."""
    if not configured(siris):
        return None
    if siris.settings.github_issues_mode == "auto":
        try:
            return file(siris, title, summary, fingerprint)
        except Exception as exc:
            logger.exception("Filing a GitHub issue failed")
            return {"error": str(exc)[:300]}
    return siris.attention.raise_item(
        f"issues:{fingerprint}",
        "suggestion",
        f"File a GitHub issue? {title}",
        source=SOURCE,
        severity="info",
        body=f"{summary}\n\nI can't fix this myself. Filing it on {siris.settings.github_issues_repo} would get it looked at.",
        actions=[action("file", "File issue", "primary"), action("dismiss", "No thanks")],
        payload={"title": title, "summary": summary, "fingerprint": fingerprint},
    )


def _file_action(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    result = file(siris, payload["title"], payload["summary"], payload["fingerprint"])
    return result, f"Filed as #{result['number']}" if result.get("action") == "filed" else f"Added to #{result['number']}"


register_action(SOURCE, "file", _file_action)
