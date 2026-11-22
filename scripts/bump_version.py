"""Bump SirisAI's version and open its changelog entry.

Versions are **Section.Feature.Fix** (e.g. 4.24.1), Brad's rule since 2026-11-22:

    python scripts/bump_version.py fix "What changed, in a line"       a fix or minor change     4.24.1 -> 4.24.2
    python scripts/bump_version.py feature "What changed, in a line"   a new feature or upgrade  4.24.1 -> 4.25.0
    python scripts/bump_version.py section "What changed, in a line"   a significant new area,   4.24.1 -> 5.0.0
                                                                       like Health

(`small` and `significant` still work, as fix and feature.) Each part counts on its own and never rolls over into the
next, so a whole-number change always means a new section.

    python scripts/bump_version.py check 4.24.1    (CI) fail unless this branch is above main's 4.24.1
    python scripts/bump_version.py rebase 4.25.0   renumber this branch's entry to follow main's 4.25.0

When another change reaches main first with the same number, `rebase` moves this branch's version, changelog heading
and newest README milestone onto the next number after main's, keeping its size (fix, feature or section).

Tagged v<version> by .github/workflows/release.yml once it reaches main. Updates app/__init__.py and adds a dated
section at the top of CHANGELOG.md with the given line; add more bullets by hand.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "app" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
KINDS = {"fix": "fix", "small": "fix", "feature": "feature", "significant": "feature", "section": "section"}
_VERSION_RE = re.compile(r'^__version__ = "(\d+\.\d+\.\d+)"$', re.M)

Version = tuple[int, int, int]


def parse(text: str) -> Version:
    parts = text.strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"A version is Section.Feature.Fix, like 4.24.1, not {text!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def fmt(v: Version) -> str:
    return f"{v[0]}.{v[1]}.{v[2]}"


def step(v: Version, kind: str) -> Version:
    kind = KINDS.get(kind, "")
    if kind == "fix":
        return v[0], v[1], v[2] + 1
    if kind == "feature":
        return v[0], v[1] + 1, 0
    if kind == "section":
        return v[0] + 1, 0, 0
    raise SystemExit("Kind must be one of: fix, feature, section")


def size(newer: Version, older: Version) -> str:
    """What kind of change took `older` to `newer`."""
    return "section" if newer[0] != older[0] else "feature" if newer[1] != older[1] else "fix"


def current() -> Version:
    match = _VERSION_RE.search(VERSION_FILE.read_text())
    if not match:
        raise SystemExit(f"No Section.Feature.Fix __version__ in {VERSION_FILE}")
    return parse(match.group(1))


def bump(kind: str, summary: str, today: date | None = None) -> str:
    if not summary.strip():
        raise SystemExit("Give a one-line summary of the change.")
    new = fmt(step(current(), kind))
    VERSION_FILE.write_text(_VERSION_RE.sub(f'__version__ = "{new}"', VERSION_FILE.read_text(), count=1))
    log = CHANGELOG.read_text()
    entry = f"## {new} — {(today or date.today()).isoformat()}\n\n- {summary.strip()}\n\n"
    first = log.find("\n## ")
    CHANGELOG.write_text(log[: first + 1] + entry + log[first + 1 :] if first >= 0 else log.rstrip() + "\n\n" + entry)
    return new


def check(base: str) -> str:
    """CI's guard against two branches claiming the same version."""
    if re.fullmatch(r"\d+\.\d+", base.strip()):  # main still on the old decimal numbers: this is the switch-over
        return f"{fmt(current())} is the first Section.Feature.Fix version (main was {base.strip()})"
    mine, theirs = current(), parse(base)
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
    headings = re.findall(r"^## (\d+\.\d+\.\d+) — ", log, re.M)
    if not headings or parse(headings[0]) != old:
        raise SystemExit("The newest CHANGELOG section doesn't match app/__init__.py.")
    kind = size(old, parse(headings[1])) if len(headings) > 1 else "fix"
    new_v = step(parse(base), kind)
    new = fmt(new_v)
    if new_v == old:
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
