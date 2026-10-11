"""Bump SirisAI's version and open its changelog entry.

    python scripts/bump_version.py small "What changed, in a line"
    python scripts/bump_version.py significant "What changed, in a line"

small        +0.01  fixes, tweaks, a setting, a small tool
significant  +0.1   a new feature or integration, a behaviour change people notice

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


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print(bump(sys.argv[1], sys.argv[2]))
