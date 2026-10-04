import asyncio
import os
from pathlib import Path

import pytest

from app.second_brain import jobs, vaults
from tests.test_second_brain_jobs import FakeBrain, FakeLLM, _at, _siris

pytestmark = pytest.mark.usefixtures("client")

REPLY = '{"notes": [{"title": "Civil 3D", "content": "Brad uses Civil 3D for pipe design.", "tags": ["CAD"]}]}'


def _write(root: Path, rel: str, text: str, mtime: float | None = None) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _vault_siris(tmp_path: Path, reply: str = REPLY, **settings):
    home, work = tmp_path / "SirisHome", tmp_path / "Work"
    home.mkdir()
    work.mkdir()
    siris = _siris(brain_vaults=f"{home}, Office={work}", brain_vaults_cache_path=str(tmp_path / "cache"), **settings)
    siris.second_brain = FakeBrain({"Civil 3D": "x"})
    siris.llm = llm = FakeLLM(reply)
    return siris, home, work, llm


def test_parse_vaults_names_bare_paths_after_their_folder() -> None:
    assert vaults.parse_vaults(" /vaults/SirisHome, Office = /vaults/Work ,") == [
        ("SirisHome", Path("/vaults/SirisHome")), ("Office", Path("/vaults/Work")),
    ]


def test_scan_skips_obsidian_folders_templates_and_conflict_copies(tmp_path: Path) -> None:
    for rel in ["Home.md", "References/Plex.md", ".obsidian/x.md", ".trash/Old.md", "Templates/Daily.md",
                "Attachments/a.md", "@eaDir/Home.md", "Home.sync-conflict-20260630-1.md",
                "Home_DiskStation_DownloadConflict.md", "Drawing.excalidraw.md"]:
        _write(tmp_path, rel, "some text")
    _write(tmp_path, "Empty.md", "  \n")
    assert sorted(n.rel_path for n in vaults.scan("Home", tmp_path)) == ["Home.md", "References/Plex.md"]


def test_added_text_is_only_the_new_lines() -> None:
    assert vaults.added_text("a\nb\nc", "a\nb changed\nc\nd\n\n") == "b changed\nd"


def test_new_notes_are_learned_with_their_source_and_marked_read(tmp_path: Path) -> None:
    siris, home, work, llm = _vault_siris(tmp_path)
    _write(work, "Civil 3D Shortcuts.md", "PIPE draws a pipe network.")
    out = asyncio.run(vaults.learn_from_vaults(siris))
    assert out == {"learned": ["Civil 3D"], "read": 1, "left": 0}
    item = siris.second_brain.learned[0]
    assert item["source"] == "Obsidian Office/Civil 3D Shortcuts.md"
    assert item["tags"] == ["cad", "obsidian", "vault-office"]
    assert "Civil 3D" in llm.messages[1]["content"] and "PIPE draws a pipe network." in llm.messages[1]["content"]
    # Nothing changed: nothing read again.
    assert asyncio.run(vaults.learn_from_vaults(siris)) == {"learned": [], "read": 0, "left": 0}


def test_a_changed_note_sends_only_what_was_added(tmp_path: Path) -> None:
    siris, home, work, llm = _vault_siris(tmp_path)
    _write(home, "Home Server.md", "Runs Docker.\nHas an RTX 3060.")
    asyncio.run(vaults.learn_from_vaults(siris))
    _write(home, "Home Server.md", "Runs Docker.\nHas an RTX 3060.\nUPS is an Eaton 5S.")
    asyncio.run(vaults.learn_from_vaults(siris))
    prompt = llm.messages[1]["content"]
    assert 'Lines just added to "Home Server"' in prompt
    assert "UPS is an Eaton 5S." in prompt and "Runs Docker." not in prompt


def test_a_note_with_only_deletions_is_marked_read_without_asking(tmp_path: Path) -> None:
    siris, home, work, llm = _vault_siris(tmp_path)
    _write(home, "Note.md", "one\ntwo")
    asyncio.run(vaults.learn_from_vaults(siris))
    llm.messages = None
    _write(home, "Note.md", "one")
    assert asyncio.run(vaults.learn_from_vaults(siris)) == {"learned": [], "read": 1, "left": 0}
    assert llm.messages is None


def test_catches_up_a_few_notes_a_night_newest_first(tmp_path: Path) -> None:
    siris, home, work, llm = _vault_siris(tmp_path, reply='{"notes": []}', brain_vaults_max_notes=2)
    for i, mtime in enumerate([100, 300, 200]):
        _write(home, f"N{i}.md", f"note {i}", mtime=mtime)
    batch, total = vaults.pending(siris)
    assert [n.rel_path for n, _ in batch] == ["N1.md", "N2.md"] and total == 3
    assert asyncio.run(vaults.learn_from_vaults(siris)) == {"learned": [], "read": 2, "left": 1}
    assert asyncio.run(vaults.learn_from_vaults(siris))["read"] == 1


def test_a_model_failure_leaves_the_note_for_next_time(tmp_path: Path) -> None:
    siris, home, work, llm = _vault_siris(tmp_path)
    _write(home, "Note.md", "text")

    async def broken(messages, **kwargs):
        raise RuntimeError("ollama down")

    llm.generate = broken
    with pytest.raises(RuntimeError):
        asyncio.run(vaults.learn_from_vaults(siris))
    assert vaults.pending(siris)[1] == 1


def test_vaults_job_runs_nightly_only_when_configured(tmp_path: Path) -> None:
    off = dict(brain_meeting_notes_hour_local=None, brain_dream_hour_local=None,
               brain_weekly_hour_local=None, brain_upkeep_hour_local=None)
    siris = _siris(**off)
    siris.second_brain = FakeBrain()
    assert asyncio.run(jobs.run_due(siris, _at(3))) == {}             # no vaults set
    siris, *_ = _vault_siris(tmp_path, **off)
    assert asyncio.run(jobs.run_due(siris, _at(0))) == {}             # before 01:00
    assert asyncio.run(jobs.run_due(siris, _at(1)))["vaults"] == {"learned": [], "read": 0, "left": 0}
    assert asyncio.run(jobs.run_due(siris, _at(4))) == {}             # once a day
