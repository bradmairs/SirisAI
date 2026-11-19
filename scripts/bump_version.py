"""Bump SirisAI's version and open its changelog entry.

    python scripts/bump_version.py small "What changed, in a line"
    python scripts/bump_version.py significant "What changed, in a line"

small        +0.01  fixes, tweaks, a setting, a small tool
significant  +0.1   a new feature or integration, a behaviour change people notice

    python scripts/bump_version.py check 6.77     (CI) fail unless this branch is above 6.77
    python scripts/bump_version.py rebase 7.17    renumber this branch's entry to follow 7.17

When another change reaches main first with the same number, `rebase` moves
this branch's version, changelog heading and newest README milestone onto
the next number after main's, keeping its size (small or significant).

Versions are decimal numbers shown with at least one decimal place (1.0,
1.01, 1.1, 1.13), and tagged v<version> by .github/workflows/release.yml
once they reach main. Updates app/__init__.py and adds a dated section at
the top of CHANGELOG.md with the given line; add more bullets by hand.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "app" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
STEPS = {"small": Decimal("0.01"), "significant": Decimal("0.1")}
_VERSION_RE = re.compile(r'^__version__ = "(\d+\.\d+)"$', re.M)


def fmt(value: Decimal) -> str:
    """1.00 -> "1.0", 1.10 -> "1.1", 1.13 -> "1.13"."""
    text = f"{value.quantize(Decimal('0.01')):f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def current() -> Decimal:
    match = _VERSION_RE.search(VERSION_FILE.read_text())
    if not match:
        raise SystemExit(f"No __version__ in {VERSION_FILE}")
    return Decimal(match.group(1))


def bump(kind: str, summary: str, today: date | None = None) -> str:
    if kind not in STEPS:
        raise SystemExit(f"Kind must be one of: {', '.join(STEPS)}")
    if not summary.strip():
        raise SystemExit("Give a one-line summary of the change.")
    new = fmt(current() + STEPS[kind])
    VERSION_FILE.write_text(_VERSION_RE.sub(f'__version__ = "{new}"', VERSION_FILE.read_text(), count=1))
    log = CHANGELOG.read_text()
    entry = f"## {new} — {(today or date.today()).isoformat()}\n\n- {summary.strip()}\n\n"
    first = log.find("\n## ")
    CHANGELOG.write_text(log[: first + 1] + entry + log[first + 1 :] if first >= 0 else log.rstrip() + "\n\n" + entry)
    return new


def check(base: str) -> str:
    """7.17: CI's guard against two branches claiming the same version."""
    mine, theirs = current(), Decimal(base)
    if mine <= theirs:
        raise SystemExit(
            f"This branch is version {fmt(mine)}, but main is already {fmt(theirs)}. "
            f"Run: python scripts/bump_version.py rebase {fmt(theirs)}"
        )
    return f"{fmt(mine)} follows main's {fmt(theirs)}"


def rebase(base: str, readme: Path | None = None) -> str:
    """Renumber this branch's newest entry to follow `base` (main's version), keeping its size."""
    old = current()
    log = CHANGELOG.read_text()
    headings = re.findall(r"^## (\d+\.\d+) — ", log, re.M)
    if not headings or Decimal(headings[0]) != old:
        raise SystemExit("The newest CHANGELOG section doesn't match app/__init__.py.")
    previous = Decimal(headings[1]) if len(headings) > 1 else old - STEPS["small"]
    step = STEPS["significant"] if old - previous >= STEPS["significant"] else STEPS["small"]
    new = fmt(Decimal(base) + step)
    if Decimal(new) == old:
        return new
    VERSION_FILE.write_text(_VERSION_RE.sub(f'__version__ = "{new}"', VERSION_FILE.read_text(), count=1))
    CHANGELOG.write_text(log.replace(f"## {fmt(old)} — ", f"## {new} — ", 1))
    readme = readme or ROOT / "README.md"
    if readme.exists():
        text = readme.read_text()
        last = list(re.finditer(r"^\d+\. \*\*Done\*\* -- .*$", text, re.M))
        if last and f"({fmt(old)})" in last[-1].group(0):
            line = last[-1].group(0).replace(f"({fmt(old)})", f"({new})", 1)
            text = text[: last[-1].start()] + line + text[last[-1].end() :]
            readme.write_text(text)
    return new


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    if sys.argv[1] == "check":
        print(check(sys.argv[2]))
    elif sys.argv[1] == "rebase":
        print(rebase(sys.argv[2]))
    else:
        print(bump(sys.argv[1], sys.argv[2]))
