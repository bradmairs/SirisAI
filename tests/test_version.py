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


def test_version_is_a_decimal_with_at_least_one_place() -> None:
    assert re.fullmatch(r"\d+\.\d{1,2}", __version__)
    assert not (len(__version__.split(".")[1]) == 2 and __version__.endswith("0"))  # 1.1, never 1.10


def test_the_changelog_has_a_section_for_the_current_version() -> None:
    """The release workflow fails without one, so a bump must come with notes."""
    assert re.search(rf"^## {re.escape(__version__)} — \d{{4}}-\d{{2}}-\d{{2}}$", (ROOT / "CHANGELOG.md").read_text(), re.M)


def test_bumps_are_hundredths_and_tenths(tmp_path: Path) -> None:
    bv = _load_bump(tmp_path)
    start = bv.current()
    small = bv.bump("small", "Fixed a thing", date(2026, 10, 12))
    assert bv.Decimal(small) == start + bv.Decimal("0.01")
    big = bv.bump("significant", "Added a big thing", date(2026, 10, 13))
    assert bv.Decimal(big) == start + bv.Decimal("0.11")
    log = (tmp_path / "CHANGELOG.md").read_text()
    assert log.index(f"## {big} — 2026-10-13") < log.index(f"## {small} — 2026-10-12") < log.index(f"## {__version__} —")
    assert "- Added a big thing" in log
    assert f'__version__ = "{big}"' in (tmp_path / "app" / "__init__.py").read_text()


def test_version_formatting(tmp_path: Path) -> None:
    from decimal import Decimal

    bv = _load_bump(tmp_path)
    assert [bv.fmt(Decimal(v)) for v in ("1", "1.00", "1.01", "1.10", "1.13", "2.0")] == ["1.0", "1.0", "1.01", "1.1", "1.13", "2.0"]


def test_a_bump_needs_a_known_kind_and_a_summary(tmp_path: Path) -> None:
    import pytest

    bv = _load_bump(tmp_path)
    with pytest.raises(SystemExit):
        bv.bump("huge", "x")
    with pytest.raises(SystemExit):
        bv.bump("small", "  ")


def test_status_and_openapi_report_the_version(client: TestClient) -> None:
    assert client.get("/siris/status").json()["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__
