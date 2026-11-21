"""Turn "I can't do that" into suggestions (7.6).

When SirisAI has no tool for a request it records a capability gap
(report_capability_gap). They used to sit in a list. On the 3rd of each
month this groups the open ones from the last 60 days by what was asked
(shared words, or the same suggested integration) and puts each group in
the feed with the most useful next step:

- **a setting to turn on**, when a part that isn't set up would cover it
  (app/upkeep/setup_check.py: "Turn on Home Assistant: SIRISAI_...");
- **it works now**, when a tool registered since covers it -- those gaps
  are marked resolved;
- **something to build**, with the integration SirisAI suggested at the time.

Dismissing a card dismisses its gaps.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.hub.attention import action, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

SOURCE = "capability_review"
_WORD = re.compile(r"[a-z]{3,}")
_STOP = frozenset("the and for you can with that this what when please could would want need like make from into have your about there".split())


def _words(text: str) -> set[str]:
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def group(gaps: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for gap in gaps:
        words = _words(gap["requested_capability"])
        for g in groups:
            head = g[0]
            same_integration = gap.get("suggested_integration") and gap["suggested_integration"] == head.get("suggested_integration")
            shared = words & _words(head["requested_capability"])
            if same_integration or len(shared) >= max(1, min(len(words), 3) // 2 + 1):
                g.append(gap)
                break
        else:
            groups.append([gap])
    return sorted(groups, key=len, reverse=True)


def next_step(siris: "SirisAI", gaps: list[dict[str, Any]]) -> dict[str, Any]:
    from app.upkeep import setup_check

    words = set().union(*(_words(g["requested_capability"]) for g in gaps))
    for f in setup_check.report(siris)["not_set_up"]:
        if words & _words(f["unlocks"] + " " + f["feature"]):
            return {"kind": "setting", "text": f"Turn on {f['feature']}: {f['settings']}. It unlocks: {f['unlocks']}."}
    specs = siris.available_tool_specs(gaps[0]["requested_capability"])
    for spec in specs:
        fn = spec["function"]
        if fn["name"] in ("remember", "recall", "web_search", "web_fetch", "get_current_time", "report_capability_gap"):
            continue
        overlap = words & _words(fn["name"].replace("_", " ") + " " + (fn.get("description") or ""))
        if len(overlap) >= 2:
            return {"kind": "works_now", "tool": fn["name"], "text": f"SirisAI can do this now ({fn['name']}). Try asking again."}
    integration = next((g["suggested_integration"] for g in gaps if g.get("suggested_integration")), None)
    return {"kind": "build", "text": f"Something to build{f': {integration}' if integration else ''}. Ask me in Claude Code and I'll add it."}


def review(siris: "SirisAI", now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=60)).replace(tzinfo=None)
    gaps = [g for g in siris.capability_gaps.list_gaps(limit=300, status="open")
            if datetime.fromisoformat(g["created_at"]).replace(tzinfo=None) >= cutoff]
    out = []
    for g in group(gaps)[:6]:
        step = next_step(siris, g)
        ids = [x["id"] for x in g]
        if step["kind"] == "works_now":
            for i in ids:
                siris.capability_gaps.mark_resolved(i, f"Covered by {step['tool']} (7.6 monthly review)")
        times = f"{len(g)} times" if len(g) > 1 else "once"
        title = f"You asked {times} for something I couldn't do: {g[0]['requested_capability'][:90]}"
        siris.attention.raise_item(
            f"{SOURCE}:{min(ids)}", "suggestion", title[:200], source=SOURCE, severity="info",
            body=step["text"] + ("\n\nAlso asked: " + "; ".join(x["requested_capability"][:80] for x in g[1:4]) if len(g) > 1 else ""),
            actions=[action("acknowledge", "Got it", "primary"), action("dismiss_gaps", "Not needed")],
            payload={"gap_ids": ids, **step}, reopen=False,
        )
        out.append({"asked": len(g), "request": g[0]["requested_capability"], **step})
    return out


def _dismiss_gaps(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    for i in payload.get("gap_ids", []):
        siris.capability_gaps.dismiss(int(i))
    return None, "Not needed"


register_action(SOURCE, "dismiss_gaps", _dismiss_gaps)
