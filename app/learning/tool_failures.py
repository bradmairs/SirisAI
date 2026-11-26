"""Bugs found by real use, kept as tests (4.29.0).

This week's live bugs came from what real services and the real model sent,
which the fake services in the test suite never did: a number as text, a
tool call in broken JSON, a key inside a URL. So every tool failure in real
use is kept (secrets already masked, app/core/secrets.py):

- **grouped** by tool and the shape of the error (digits and quoted values
  ignored), counting how often each happens;
- **a code bug** -- TypeError, AttributeError, KeyError, IndexError, or a
  ValueError from SirisAI's own code -- is SirisAI's fault, not the
  service's. The second time one happens, it's raised in the feed and, with
  GitHub issues set up, proposed as an issue (app/upkeep/issues.py);
- **exported as replay cases** (`GET /siris/learning/tool-failures?format=replay`):
  the tool and the exact arguments it was given. Saved under
  tests/replays/, `tests/test_replays.py` checks each still gets through
  argument checking for that tool (app/core/tool_args.py) -- the class of
  bug 4.27.1 fixed. The service's own reply isn't recorded, so a replay
  checks how the call is made, not what the service sends back.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

STATE_KEY = "tool_failures"
MAX = 150
CODE_BUGS = frozenset({"TypeError", "AttributeError", "KeyError", "IndexError", "UnboundLocalError", "NameError", "ZeroDivisionError"})
_SHAPE = re.compile(r"\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"")
_ENVIRONMENT = re.compile(r"connect|timed? ?out|refused|unreachable|name resolution|503|502|504|ssl|certificate|network", re.I)


def shape(error: str) -> str:
    return _SHAPE.sub("_", error or "")[:120]


def fingerprint(tool: str, error: str) -> str:
    return hashlib.sha1(f"{tool}|{shape(error)}".encode()).hexdigest()[:12]


def is_code_bug(kind: str, error: str) -> bool:
    if kind in CODE_BUGS:
        return True
    return kind == "ValueError" and not _ENVIRONMENT.search(error or "") and "invalid literal" in (error or "")


def record(siris: "SirisAI", tool: str, arguments: dict[str, Any], error: str, kind: str = "Exception") -> dict[str, Any]:
    from app.core.secrets import scrub

    items: dict[str, dict[str, Any]] = dict(siris.state.get(STATE_KEY) or {})
    key = fingerprint(tool, error)
    now = datetime.now(UTC).isoformat()
    entry = items.get(key) or {"tool": tool, "kind": kind, "error": error[:400], "first": now, "count": 0}
    entry.update({"arguments": scrub(arguments), "last": now, "count": entry["count"] + 1, "code_bug": is_code_bug(kind, error)})
    items[key] = entry
    if len(items) > MAX:
        for old in sorted(items, key=lambda k: items[k]["last"])[: len(items) - MAX]:
            items.pop(old, None)
    siris.state.set(STATE_KEY, items)
    if entry["code_bug"] and entry["count"] == 2:
        _escalate(siris, key, entry)
    return entry


def _escalate(siris: "SirisAI", key: str, entry: dict[str, Any]) -> None:
    from app.upkeep.alerts import raise_alert

    title = f"A bug in {entry['tool']}: {entry['kind']}"
    body = (f"{entry['tool']} failed twice the same way, which is a fault in SirisAI's own code rather than the service:\n"
            f"{entry['kind']}: {entry['error'][:300]}\nLast arguments: {entry['arguments']}")
    raise_alert(siris, f"tool_bug:{key}", title, body, severity="attention", issue=True)


def recent(siris: "SirisAI", limit: int = 50) -> list[dict[str, Any]]:
    items = list((siris.state.get(STATE_KEY) or {}).items())
    items.sort(key=lambda kv: kv[1]["last"], reverse=True)
    return [{"fingerprint": k, **v} for k, v in items[:limit]]


def replay_cases(siris: "SirisAI") -> list[dict[str, Any]]:
    return [{"tool": e["tool"], "arguments": e["arguments"], "error": e["error"], "kind": e["kind"]} for e in recent(siris, MAX)]
