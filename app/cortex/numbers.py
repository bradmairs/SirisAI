"""Check the numbers before answering (7.27).

A reply built on looked-up data ("it's 14 degrees", "3 parcels", "61% full")
can still get a number wrong: a small model rounds badly, mixes up two
readings or invents one. After a turn whose tools returned numbers, every
number in the reply must be found in that data (allowing for rounding, a
count of a list, or a fraction shown as a percent). If one isn't, the local
model is asked once, given the data, for a one-sentence correction, which
is added to the answer ("Correction: ...") -- so a spoken reply hears it
straight after. If the model says the numbers were right (worked out from
the data, say), nothing is added.

Numbers that need no checking are skipped: ones you said yourself, years,
clock times and dates, and small counts up to 10.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

_NUMBER = re.compile(r"(?<![\w.:/-])-?\d+(?:[.,]\d+)?(?![\w:/])")
_SKIP = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}(?:am|pm)\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", re.I)
SUPPORTED_TOLERANCE = 0.01


def _numbers(text: str) -> list[float]:
    cleaned = _SKIP.sub(" ", text)
    out = []
    for m in _NUMBER.findall(cleaned):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def _data_numbers(value: Any, out: set[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.add(float(value))
    elif isinstance(value, str):
        out.update(_numbers(value))
    elif isinstance(value, dict):
        for v in value.values():
            _data_numbers(v, out)
    elif isinstance(value, (list, tuple)):
        out.add(float(len(value)))
        for v in value:
            _data_numbers(v, out)


def _supported(n: float, data: set[float]) -> bool:
    for d in data:
        if abs(n - d) <= max(0.5, abs(d) * SUPPORTED_TOLERANCE):
            return True
        if 0 < abs(d) <= 1 and abs(n - d * 100) <= 0.5:   # 0.61 shown as 61%
            return True
        if abs(d) >= 1000 and abs(n - d / 1000) <= 0.5:     # 2048 MB as 2 GB, 1500 m as 1.5 km
            return True
    return False


def unsupported(reply: str, results: list[Any], prompt: str = "") -> list[float]:
    """Numbers in `reply` that none of the tool `results` back up."""
    data: set[float] = set()
    for r in results:
        _data_numbers(r, data)
    if not data:
        return []
    asked = set(_numbers(prompt))
    out = []
    for n in _numbers(reply):
        if n in asked or (1900 <= n <= 2100 and n == int(n)) or (0 <= n <= 10 and n == int(n)):
            continue
        if not _supported(n, data) and n not in out:
            out.append(n)
    return out


def _fmt(n: float) -> str:
    return f"{n:g}"


async def check(siris: "SirisAI", result: Any, prompt: str) -> None:
    """Add a correction to `result.response` when its numbers aren't in the data."""
    if not siris.settings.check_numbers or result.error or not result.response or siris.llm is None:
        return
    outcomes = list(result.plan) + ([result.tool_call] if result.tool_call is not None else [])
    results = [o.result for o in outcomes if o.result is not None]
    if not results:
        return
    missing = unsupported(result.response, results, prompt)
    if not missing:
        return
    data = json.dumps(results, default=str)[:6000]
    ask = [
        {"role": "system", "content": "You check answers against data. Be exact and brief."},
        {"role": "user", "content": (
            f"Question: {prompt}\n\nData: {data}\n\nAnswer given: {result.response}\n\n"
            f"These numbers in the answer weren't found in the data: {', '.join(_fmt(n) for n in missing)}. "
            "If the answer is right (for example they were worked out correctly from the data), reply exactly OK. "
            "Otherwise reply with one short sentence giving the correct figures from the data, nothing else."
        )},
    ]
    try:
        reply = await siris.llm.generate(ask)
    except Exception:
        logger.warning("Number check failed", exc_info=True)
        return
    text = (reply.text or "").strip()
    if not text or text.upper().rstrip(".!") == "OK" or text.upper().startswith("OK"):
        return
    result.response = f"{result.response.rstrip()}\n\nCorrection: {text}"
    result.number_check = {"unsupported": missing, "corrected": True}
