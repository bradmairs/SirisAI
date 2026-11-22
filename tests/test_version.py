"""The version (app/__init__.py), its bump script and the changelog the
release workflow reads (see CLAUDE.md)."""

import importlib.util
import re
import shutil
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__

ROOT = Path(__file__).resolve().parents[1]


def _load_bump(tmp: Path):
    (tmp / "app").mkdir()
    (tmp / "scripts").mkdir()
    shutil.copy(ROOT / "app" / "__init__.py", tmp / "app" / "__init__.py")
    shutil.copy(ROOT / "CHANGELOG.md", tmp / "CHANGELOG.md")
    shutil.copy(ROOT / "scripts" / "bump_version.py", tmp / "scripts" / "bump_version.py")
    spec = importlib.util.spec_from_file_location("bump_version", tmp / "scripts" / "bump_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_version_is_section_feature_fix() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)  # e.g. 4.24.1 (CLAUDE.md, since 2026-11-22)


def test_the_changelog_has_a_section_for_the_current_version() -> None:
    """The release workflow fails without one, so a bump must come with notes."""
    assert re.search(rf"^## {re.escape(__version__)} — \d{{4}}-\d{{2}}-\d{{2}}$", (ROOT / "CHANGELOG.md").read_text(), re.M)


def test_fix_feature_and_section_bumps(tmp_path: Path) -> None:
    bv = _load_bump(tmp_path)
    s, f, x = bv.current()
    fix = bv.bump("fix", "Fixed a thing", date(2026, 10, 12))
    assert fix == f"{s}.{f}.{x + 1}"
    feature = bv.bump("feature", "Added a thing", date(2026, 10, 13))
    assert feature == f"{s}.{f + 1}.0"
    section = bv.bump("section", "A whole new area", date(2026, 10, 14))
    assert section == f"{s + 1}.0.0"
    assert bv.bump("small", "Old word for a fix", date(2026, 10, 15)) == f"{s + 1}.0.1"
    log = (tmp_path / "CHANGELOG.md").read_text()
    assert log.index(f"## {section} — 2026-10-14") < log.index(f"## {feature} — 2026-10-13") < log.index(f"## {fix} — 2026-10-12") < log.index(f"## {__version__} —")
    assert "- Added a thing" in log
    assert f'__version__ = "{s + 1}.0.1"' in (tmp_path / "app" / "__init__.py").read_text()


def test_parts_never_roll_over(tmp_path: Path) -> None:
    bv = _load_bump(tmp_path)
    assert bv.step((4, 9, 9), "fix") == (4, 9, 10)
    assert bv.step((4, 9, 9), "feature") == (4, 10, 0)
    assert bv.fmt((4, 10, 0)) == "4.10.0"


def test_a_bump_needs_a_known_kind_and_a_summary(tmp_path: Path) -> None:
    import pytest

    bv = _load_bump(tmp_path)
    with pytest.raises(SystemExit):
        bv.bump("huge", "x")
    with pytest.raises(SystemExit):
        bv.bump("fix", "  ")


def test_status_and_openapi_report_the_version(client: TestClient) -> None:
    assert client.get("/siris/status").json()["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__
