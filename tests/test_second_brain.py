import json
import shutil
import uuid
import subprocess
import textwrap
from pathlib import Path

import pytest

from app.llm.base import LLMResponse

from app.core.config import Settings
from app.core.health import check_integrations
from app.core.siris import SirisAI
from app.memory.entities import EntityMemoryStore
from app.plugins.builtin.memory import RememberEntityFactTool
from app.plugins.builtin.second_brain import (
    MEMORY_INBOX_TITLE,
    BrainGetNoteTool,
    BrainInsightsTool,
    BrainLearnTool,
    BrainSearchTool,
    mirror_to_brain,
)
from app.second_brain.client import SecondBrainClient, SecondBrainError

# A stand-in for the vault's real tools/brain.py: same subcommands and JSON
# shapes, backed by a notes.json file, and every call logged to calls.jsonl
# so tests can see exactly what the client sent. Learning under an alias
# lands on the real note, like the real engine.
FAKE_ENGINE = textwrap.dedent(
    '''
    import json, pathlib, sys
    root = pathlib.Path(__file__).resolve().parent.parent
    args = sys.argv[1:]
    stdin = sys.stdin.read() if "--json" in args or args[:1] == ["edit"] or "-" in args else ""
    with open(root / "calls.jsonl", "a") as f:
        f.write(json.dumps({"args": args, "stdin": stdin}) + "\\n")
    store = root / "notes.json"
    notes = json.loads(store.read_text()) if store.exists() else {}
    aliases = {"dom": "Dom Mairs"}

    def find(title):
        if not title:
            return "Siris Memory Inbox" if "Siris Memory Inbox" in notes else None
        title = aliases.get(title.lower(), title)
        return next((t for t in notes if t.lower() == title.lower()), None)

    cmd = args[0]
    if cmd == "graph":
        (root / "_site").mkdir(exist_ok=True)
        (root / "_site" / "index.html").write_text("<html>map of " + str(len(notes)) + " notes</html>")
        print("Mind map -> _site/index.html")
        sys.exit(0)
    if cmd == "list":
        print(json.dumps([{"title": "Werribee Drainage", "type": "project", "aliases": ["WD"], "path": "01-Projects/Werribee Drainage.md", "status": ""},
                          {"title": "Plex", "type": "resource", "aliases": [], "path": "03-Resources/Plex.md", "status": ""},
                          {"title": "Akaroa Upgrade", "type": "project", "aliases": [], "path": "01-Projects/Akaroa Upgrade.md", "status": "done"}]))
        sys.exit(0)
    if cmd == "tasks":
        print(json.dumps([{"task": "Send levels to Dana", "note": "Werribee Drainage", "due": "2026-10-20", "type": "project"}]))
        sys.exit(0)
    if cmd == "insights":
        days = int(args[args.index("--days") + 1])
        print(json.dumps({"date": "2026-10-03", "highlights": [f"{len(notes)} notes"], "summary": {"notes": len(notes)},
                          "activity": [{"date": "2026-10-03", "notes": 1}] * days}))
        sys.exit(0)
    if cmd == "fail":
        sys.exit("engine exploded")
    if cmd == "inbox":
        print(json.dumps({"memory": [{"id": "a1", "date": "2026-10-03", "text": "x", "plain": "x"}], "notes": []}))
        sys.exit(0)
    if cmd == "file":
        t = args[args.index("--") + 1]
        (root / "Notes").mkdir(exist_ok=True)
        (root / "Notes" / f"{t}.md").write_text("filed")
        print(json.dumps({"action": "filed", "title": t, "type": args[args.index("--type") + 1]}))
        sys.exit(0)
    if cmd == "clear-memory-inbox":
        (root / "archive.md").write_text(stdin)
        print(json.dumps({"archived": len(json.loads(stdin))}))
        sys.exit(0)
    if cmd in ("delete", "restore", "purge"):
        t = args[args.index("--") + 1]
        trash = json.loads((root / "trash.json").read_text()) if (root / "trash.json").exists() else {}
        if cmd == "delete":
            real = find(t)
            if real is None:
                sys.exit("No note called " + repr(t))
            if real == "Health":
                sys.exit("Health is one of the hubs the brain is organised around, so it can't be deleted")
            trash[real] = notes.pop(real)
            result = {"action": "deleted", "title": real, "path": f"Notes/{real}.md", "permanent": "--permanent" in args,
                      "deleted_to": None if "--permanent" in args else f".deleted/2026-10-16/Notes/{real}.md", "linked_from": []}
        else:
            real = next((x for x in trash if x.lower() == t.lower()), None)
            if real is None:
                sys.exit("No deleted note called " + repr(t))
            body = trash.pop(real)
            if cmd == "restore":
                notes[real] = body
            result = {"action": "restored" if cmd == "restore" else "purged", "title": real, "path": f"Notes/{real}.md", "relinked": []}
        store.write_text(json.dumps(notes))
        (root / "trash.json").write_text(json.dumps(trash))
        print(json.dumps(result))
        sys.exit(0)
    if cmd == "deleted":
        trash = json.loads((root / "trash.json").read_text()) if (root / "trash.json").exists() else {}
        print(json.dumps([{"title": t, "type": "note", "path": f"Notes/{t}.md", "date": "2026-10-16", "linked_from": [],
                           "restorable": True} for t in trash]))
        sys.exit(0)
    if cmd in ("link", "unrelate"):
        a, b = (find(x) for x in args[args.index("--") + 1:])
        if a is None or b is None:
            sys.exit("No note called " + repr(args[-2] if a is None else args[-1]))
        if cmd == "link":
            notes[a] += f"\\n- [[{b}]]"
            notes[b] += f"\\n- [[{a}]]"
            store.write_text(json.dumps(notes))
        else:
            (root / ".not-related.json").write_text(json.dumps([sorted((a, b))]))
        print(json.dumps({"action": "linked" if cmd == "link" else "dismissed", "a": a, "b": b, "changed": [a, b]}))
        sys.exit(0)
    if cmd == "autolink":
        print(json.dumps({"linked": [{"a": "Pump Stations", "b": "Rising Mains", "confidence": 88, "why": "88% similar"}],
                          "deferred": 0, "thresholds": {"auto_min": 0.8, "basis": "default"}}))
        sys.exit(0)
    if cmd == "done":
        rest = args[args.index("--") + 1:]
        real = find(rest[0])
        if real is None:
            sys.exit("No note called " + repr(rest[0]))
        print(json.dumps({"action": "done" if len(rest) > 1 else "project-done", "note": real, "task": rest[1] if len(rest) > 1 else None}))
        sys.exit(0)
    if cmd in ("merge", "distinct"):
        a, b = (find(x) for x in args[args.index("--") + 1:])
        if a is None or b is None:
            sys.exit("No note called " + repr(args[-2] if a is None else args[-1]))
        print(json.dumps({"action": "merged", "from": a, "into": b} if cmd == "merge" else {"action": "distinct", "a": a, "b": b}))
        sys.exit(0)
    if cmd == "recent":
        folder = args[args.index("--folder") + 1]
        print(json.dumps([{"title": t, "path": f"Work/{t}.md", "type": "note", "as_of": "2026-11-06", "tags": [], "excerpt": b[:50]}
                          for t, b in notes.items() if folder == "Work"]))
        sys.exit(0)
    if cmd == "edit":
        t = find(args[args.index("--") + 1])
        if t is None or ("--expect" in args and args[args.index("--expect") + 1] != "v1"):
            print("No note called that." if t is None else t + " changed since you opened it.", file=sys.stderr)
            sys.exit(1)
        notes[t] = stdin
        store.write_text(json.dumps(notes))
        print(json.dumps({"action": "edited", "title": t, "path": f"Notes/{t}.md", "links_added": [], "version": "v2"}))
        sys.exit(0)
    if cmd == "handwriting":
        print(json.dumps([{"title": "Site Visit", "path": "Work/WSP/Site Visit.md", "source": "OneNote: WSP › Site › Site Visit",
                           "score": 6.5, "illegible": 1, "odd_words": ["meroh"], "text": "Dellers underslip rear Meroh"}]))
        sys.exit(0)
    if cmd == "handwriting-check":
        title = args[args.index("--") + 1]
        print(json.dumps({"action": "corrected" if "--text" in args else "checked", "title": title, "text": stdin}))
        sys.exit(0)
    if cmd == "set-block":
        note, name, _ = args[args.index("--") + 1:]
        print(json.dumps({"action": "set", "title": note, "text": stdin}))
        sys.exit(0)
    if cmd == "add-line":
        note, heading, line = args[args.index("--") + 1:]
        print(json.dumps({"action": "added", "title": note}))
        sys.exit(0)
    if cmd == "duplicates":
        print(json.dumps([{"a": "GVW Project Closure", "b": "GVW Contract Closure", "score": 0.9, "why": "90% similar"}]))
        sys.exit(0)
    if cmd == "project-pages":
        print(json.dumps([{"project": "Melrose East Pump Station", "action": "updated", "path": "Work/GVW/Projects/x.md",
                           "mentions": ["GVW Budgets"], "tasks": 0, "people": []}]))
        sys.exit(0)
    if cmd == "unlink":
        a, b = (find(x) for x in args[args.index("--") + 1:])
        if a is None or b is None:
            sys.exit("No note called " + repr(args[-2] if a is None else args[-1]))
        print(json.dumps({"action": "unlinked", "a": a, "b": b, "changed": [a, b]}))
        sys.exit(0)
    if cmd == "log":
        log = root / "activity.json"
        if not log.exists():
            sys.exit("invalid choice: 'log'")   # an older vault, from before the activity log
        date = args[args.index("--date") + 1]
        print(json.dumps([e for e in json.loads(log.read_text()) if e["date"] == date]))
        sys.exit(0)
    if cmd == "review":
        if "--" in args:
            print(json.dumps({"reviewed": True, "title": args[args.index("--") + 1], "date": "2026-10-17"}))
        else:
            n = int(args[args.index("--count") + 1])
            print(json.dumps([{"title": t, "path": f"Notes/{t}.md", "links": 3, "as_of": "2026-10-02", "excerpt": b[:40]}
                              for t, b in list(notes.items())[:n]]))
        sys.exit(0)
    if cmd == "relink":
        print("Notes/A.md: + B\\nAdded 3 links.")
        sys.exit(0)
    if cmd == "search":
        q = args[args.index("--") + 1].lower()
        hits = [{"title": t, "path": f"Notes/{t}.md", "type": "note", "tags": [], "score": 5, "excerpts": [b[:80]],
                 "as_of": "2026-10-02"}
                for t, b in notes.items() if q in t.lower() or q in b.lower()]
        print(json.dumps(hits))
    elif cmd == "show":
        t = find(args[args.index("--") + 1])
        print(json.dumps(None if t is None else {"title": t, "path": f"Notes/{t}.md", "meta": {}, "aliases": [],
                                                  "tags": [], "body": notes[t], "links": [], "backlinks": []}))
    elif cmd == "route":
        t = find(args[args.index("--") + 1])
        print(json.dumps({"action": "update" if t else "create", "title": t or args[args.index("--") + 1],
                          "type": "person" if t == "Dom Mairs" else "note", "folder": "People" if t == "Dom Mairs" else "Notes",
                          "areas": [] if t else ["Health"], "confidence": 0.9, "reasons": ["fake"]}))
    elif cmd == "learn":
        results = []
        for item in json.loads(stdin):
            t = find(item["title"]) or (item["title"] or "Siris Memory Inbox")
            if t in notes:
                notes[t] += "\\n\\n### update\\n" + item["content"]
                action = "updated"
            else:
                notes[t] = f"# {t}\\n\\n" + item["content"].replace("Plex", "[[Plex]]")
                action = "learned"
            (root / "Notes").mkdir(exist_ok=True)
            (root / "Notes" / f"{t}.md").write_text(notes[t])
            results.append({"action": action, "title": t, "path": f"Notes/{t}.md", "links": [], "mentioned_in": 0,
                            "areas": ["Relationships"] if t == "Dom Mairs" else []})
        store.write_text(json.dumps(notes))
        print(json.dumps(results))
    '''
)


def _vault(path: Path, notes: dict[str, str] | None = None) -> Path:
    (path / "tools").mkdir(parents=True, exist_ok=True)
    (path / "tools" / "brain.py").write_text(FAKE_ENGINE)
    if notes is not None:
        (path / "notes.json").write_text(json.dumps(notes))
    return path


def _calls(vault: Path) -> list[dict]:
    log = vault / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# ---------- client ----------


def test_search_passes_query_after_double_dash(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Home Server": "i5, RTX 3060, Ubuntu"})
    client = SecondBrainClient(str(vault))
    hits = client.search("rtx", limit=3)
    assert [h["title"] for h in hits] == ["Home Server"]
    assert _calls(vault)[-1]["args"] == ["search", "--limit", "3", "--output", "json", "--", "rtx"]


def test_query_that_looks_like_a_flag_is_still_a_query(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Notes": "--limit is a word here"})
    assert SecondBrainClient(str(vault)).search("--limit") != []


def test_get_note_returns_none_when_missing(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    assert SecondBrainClient(str(vault)).get_note("Nothing") is None


def test_learn_sends_items_on_stdin(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    results = SecondBrainClient(str(vault)).learn([{"title": "Plex", "content": "Media server.", "tags": ["media"]}])
    assert results[0]["action"] == "learned"
    call = _calls(vault)[-1]
    assert call["args"] == ["learn", "--json", "-", "--output", "json"]
    assert json.loads(call["stdin"]) == [{"title": "Plex", "content": "Media server.", "tags": ["media"]}]


def test_missing_engine_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(SecondBrainError, match="SIRISAI_BRAIN_PATH"):
        SecondBrainClient(str(tmp_path)).search("x")


def test_engine_failure_raises_with_its_message(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    with pytest.raises(SecondBrainError, match="engine exploded"):
        SecondBrainClient(str(vault))._engine_call(["fail"])


# ---------- tools ----------


def test_search_and_get_note_tools(tmp_path: Path) -> None:
    client = SecondBrainClient(str(_vault(tmp_path, {"Dom Mairs": "Brad's wife."})))
    assert BrainSearchTool(client).run(query="wife")[0]["title"] == "Dom Mairs"
    note = BrainGetNoteTool(client).run(title="Dom")
    assert note["found"] is True and note["title"] == "Dom Mairs"
    assert BrainGetNoteTool(client).run(title="Nobody")["found"] is False


def test_learn_tool_saves_and_verifies_despite_added_links(tmp_path: Path) -> None:
    client = SecondBrainClient(str(_vault(tmp_path, {})))
    tool = BrainLearnTool(client)
    args = {"title": "Tautulli", "content": "Tracks what's playing on Plex."}
    result = tool.run(**args)
    assert result["saved"] is True and result["action"] == "learned"
    # The engine linked "Plex" -> "[[Plex]]"; verification compares without link markup.
    assert tool.verify(args, result).verified is True


def test_learn_tool_needs_title_and_content(tmp_path: Path) -> None:
    tool = BrainLearnTool(SecondBrainClient(str(_vault(tmp_path, {}))))
    assert tool.run(title="", content="x")["saved"] is False
    assert _calls(tmp_path) == []


def test_learn_dry_run_shows_the_routers_decision(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"Dom Mairs": "Brad's wife."})
    tool = BrainLearnTool(SecondBrainClient(str(vault)))
    assert tool.dry_run(title="Dom", content="x") == {
        "title": "Dom", "would_create": False, "would_update": True, "existing_title": "Dom Mairs",
        "path": "People/Dom Mairs.md", "suggested_path": "People/Dom Mairs.md", "type": "person", "areas": [],
        "reasons": ["fake"],
    }
    new = tool.dry_run(title="Knee Soreness", content="sore after runs")
    assert new["would_create"] is True and new["areas"] == ["Health"]
    # Something new waits in the inbox for the nightly sort; the router's guess is the suggestion.
    assert (new["path"], new["suggested_path"]) == ("00-Inbox/Knee Soreness.md", "Notes/Knee Soreness.md")
    assert _calls(vault)[-1]["args"] == ["route", "--type", "auto", "--output", "json", "-c", "sore after runs", "--", "Knee Soreness"]


def test_learn_tool_leaves_the_type_to_the_router_by_default(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    BrainLearnTool(SecondBrainClient(str(vault))).run(title="Knee Soreness", content="sore after runs")
    assert "type" not in json.loads(_calls(vault)[-1]["stdin"])[0]
    assert json.loads(_calls(vault)[-1]["stdin"])[0]["inbox_if_new"] is True


def test_learn_tool_only_passes_known_types(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    BrainLearnTool(SecondBrainClient(str(vault))).run(title="X", content="y", type="spaceship")
    assert "type" not in json.loads(_calls(vault)[-1]["stdin"])[0]
    assert json.loads(_calls(vault)[-1]["stdin"])[0]["inbox_if_new"] is True


# ---------- memory mirroring ----------


# Tests that build a full SirisAI use the session `client` fixture, which
# creates the shared test database's tables (see conftest.py).
needs_db = pytest.mark.usefixtures("client")


def _entities() -> EntityMemoryStore:
    return SirisAI(settings=Settings()).entities


@needs_db
def test_entity_fact_is_mirrored_onto_the_entity_note(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault", {"Dom Mairs": "Brad's wife."})
    tool = RememberEntityFactTool(_entities(), brain=SecondBrainClient(str(vault)))
    result = tool.run(entity_name="Dom", fact=f"Loves trail running {uuid.uuid4().hex}.")
    assert result["second_brain"] == {"saved": True, "action": "updated", "title": "Dom Mairs", "path": "Notes/Dom Mairs.md",
                                      "areas": ["Relationships"]}
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["tags"] == ["siris-memory"]


@needs_db
def test_duplicate_entity_fact_is_not_mirrored_twice(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault", {})
    tool = RememberEntityFactTool(_entities(), brain=SecondBrainClient(str(vault)))
    fact = f"Runs on the home server {uuid.uuid4().hex}."  # the suite shares one database
    tool.run(entity_name="Plex", fact=fact)
    second = tool.run(entity_name="Plex", fact=fact)
    assert second.get("duplicate") is True and "second_brain" not in second
    assert len([c for c in _calls(vault) if c["args"][0] == "learn"]) == 1


@needs_db
def test_vault_failure_never_fails_the_memory(tmp_path: Path) -> None:
    broken = SecondBrainClient(str(tmp_path / "no-vault"))
    tool = RememberEntityFactTool(_entities(), brain=broken)
    fact = f"Port 32400 {uuid.uuid4().hex}."
    result = tool.run(entity_name="Plex", fact=fact)
    assert result["fact"] == fact
    assert result["second_brain"]["saved"] is False
    assert "SecondBrainError" in result["second_brain"]["error"]


@needs_db
def test_free_text_memory_goes_to_the_inbox(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault", {})
    siris = SirisAI(settings=Settings(brain_path=str(vault)))
    result = siris.tools.get("remember").run(content=f"Brad prefers flat whites {uuid.uuid4().hex}.")
    assert result["second_brain"]["title"] == MEMORY_INBOX_TITLE
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["title"] == "" and "type" not in sent   # the vault's router decides where it goes


@needs_db
def test_mirroring_can_be_turned_off(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault", {})
    siris = SirisAI(settings=Settings(brain_path=str(vault), brain_mirror_memory=False))
    assert "second_brain" not in siris.tools.get("remember").run(content=f"Something {uuid.uuid4().hex}.")
    assert _calls(vault) == []


def test_mirror_without_a_brain_is_a_no_op() -> None:
    assert mirror_to_brain(None, "X", "y") is None


# ---------- registration + health ----------


@needs_db
def test_brain_tools_registered_only_when_configured(tmp_path: Path) -> None:
    assert SirisAI(settings=Settings()).tools.get("brain_search") is None
    siris = SirisAI(settings=Settings(brain_path=str(tmp_path)))
    assert {"brain_search", "brain_get_note", "brain_insights", "brain_learn"} <= {t.name for t in siris.tools.list()}


def test_health_reports_the_vault(tmp_path: Path) -> None:
    missing = [r for r in check_integrations(Settings(brain_path=str(tmp_path))) if r["name"] == "Siris"]
    assert missing == [{"name": "Siris", "ok": False, "detail": f"no tools/brain.py under {tmp_path}", "ms": None}]
    present = [r for r in check_integrations(Settings(brain_path=str(_vault(tmp_path)))) if r["name"] == "Siris"]
    assert present[0]["ok"] is True


# ---------- git sync (real git, local bare remote) ----------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _synced_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'GitHub' remote with the fake vault pushed to it, and a clone of it."""
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _vault(seed, {})
    _git(seed, "init", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(remote), str(clone))
    return remote, clone


@needs_git
def test_learn_commits_and_pushes(tmp_path: Path) -> None:
    remote, clone = _synced_pair(tmp_path)
    client = SecondBrainClient(str(clone), git_sync=True)
    client.learn([{"title": "Plex", "content": "Media server."}])
    client.flush()
    assert _git(remote, "log", "-1", "--format=%s", "main") == "siris: learned Plex"
    assert client.last_sync_error is None


def test_inbox_file_clear_and_relink(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = SecondBrainClient(str(vault))
    assert client.inbox()["memory"][0]["id"] == "a1"
    assert client.file_note("-odd title", type="project") == {"action": "filed", "title": "-odd title", "type": "project"}
    assert client.clear_memory_inbox([{"id": "a1", "filed_to": "Dom Mairs"}]) == {"archived": 1}
    assert client.relink() == 3
    calls = _calls(vault)
    assert calls[1]["args"][-2:] == ["--", "-odd title"]   # a title starting with "-" is never read as a flag
    assert json.loads(calls[2]["stdin"]) == [{"id": "a1", "filed_to": "Dom Mairs"}]


@needs_git
def test_upkeep_writes_are_committed(tmp_path: Path) -> None:
    remote, clone = _synced_pair(tmp_path)
    client = SecondBrainClient(str(clone), git_sync=True)
    client.file_note("Knee Soreness")
    client.flush()
    assert _git(remote, "log", "-1", "--format=%s", "main") == "siris: filed Knee Soreness"


@needs_git
def test_push_rebases_over_someone_elses_push(tmp_path: Path) -> None:
    remote, clone = _synced_pair(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    (other / "Daily.md").write_text("written elsewhere")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "from a Claude Code session")
    _git(other, "push")

    client = SecondBrainClient(str(clone), git_sync=True, pull_interval=3600)
    client._last_pull = float("inf")  # pretend we pulled just now, so the push is what meets the conflict
    client.learn([{"title": "Plex", "content": "Media server."}])
    client.flush()
    log = _git(remote, "log", "--format=%s", "main").splitlines()
    assert log[:2] == ["siris: learned Plex", "from a Claude Code session"]


@needs_git
def test_sync_failure_is_kept_for_health_not_raised(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault", {})
    _git(vault, "init", "-b", "main")  # no remote: pull and push both fail
    client = SecondBrainClient(str(vault), git_sync=True)
    assert client.learn([{"title": "Plex", "content": "Media server."}])[0]["action"] == "learned"
    client.flush()
    assert client.last_sync_error is not None and client.last_sync_error.startswith("git ")
    health = [r for r in check_integrations(Settings(brain_path=str(vault)), second_brain=client) if r["name"] == "Siris"]
    assert health[0]["ok"] is False and health[0]["detail"] == client.last_sync_error


@needs_git
def test_reads_pull_in_the_background(tmp_path: Path) -> None:
    remote, clone = _synced_pair(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    (other / "notes.json").write_text(json.dumps({"Running": "10 km on 10 October."}))
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "from a Claude Code session")
    _git(other, "push")

    client = SecondBrainClient(str(clone), git_sync=True)
    client.search("10 km")  # answers from the local copy straight away and starts a pull
    client.flush()
    assert [h["title"] for h in client.search("10 km")] == ["Running"]
    assert client.last_sync_error is None


# ---------- chat context ----------


class _FakeBrain:
    def __init__(self, hits=None, fail=False):
        self.hits, self.fail, self.queries = hits or [], fail, []

    def search(self, query, limit=10):
        self.queries.append((query, limit))
        if self.fail:
            raise SecondBrainError("vault gone")
        return self.hits[:limit]


@needs_db
def test_context_includes_strong_brain_matches_only() -> None:
    siris = SirisAI(settings=Settings())
    siris.second_brain = _FakeBrain([
        {"title": "Melbourne Marathon 2026 10 km", "score": 6, "excerpts": ["Target under 1:10:00"], "as_of": "2026-10-05"},
        {"title": "Home", "score": 1, "excerpts": ["one weak word"]},
    ])
    text = siris.context.build(siris, "how's my 10 km prep going?")
    assert "Relevant notes from Siris" in text
    assert "[Melbourne Marathon 2026 10 km, as of 2026-10-05] Target under 1:10:00" in text
    assert "[Home]" not in text
    assert siris.second_brain.queries == [("how's my 10 km prep going?", 3)]


@needs_db
def test_context_brain_notes_can_be_turned_off() -> None:
    siris = SirisAI(settings=Settings(brain_context_notes=0))
    siris.second_brain = _FakeBrain([{"title": "X", "score": 9, "excerpts": ["y"]}])
    assert "Relevant notes from Siris" not in siris.context.build(siris, "anything")
    assert siris.second_brain.queries == []


@needs_db
def test_a_broken_vault_never_breaks_chat_context() -> None:
    siris = SirisAI(settings=Settings())
    siris.second_brain = _FakeBrain(fail=True)
    text = siris.context.build(siris, "hello")
    assert text is not None and "Relevant notes from Siris" not in text


def test_engine_env_reaches_the_engine(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    (vault / "tools" / "brain.py").write_text(
        "import json, os\nprint(json.dumps([{'title': os.environ.get('BRAIN_EMBED_MODEL', ''), 'path': '', 'type': '', 'tags': [], 'score': 1, 'excerpts': []}]))\n"
    )
    client = SecondBrainClient(str(vault), engine_env={"BRAIN_EMBED_MODEL": "nomic-embed-text"})
    assert client.search("x")[0]["title"] == "nomic-embed-text"


# ---------- HTTP: capture, map, today ----------

from fastapi.testclient import TestClient  # noqa: E402

from app.api.routes.brain import capture_title  # noqa: E402
from app.core.siris import get_siris  # noqa: E402


@pytest.fixture
def brain_api(client: TestClient, tmp_path: Path):
    siris = get_siris()
    vault = _vault(tmp_path / "vault", {
        "2026-10-02": "## Log\n- 🧠 Learned [[Plex]]\n- 🧠 Updated [[Dom Mairs]]\n- 🧠 Updated [[Plex]]",
    })
    previous = siris.second_brain
    siris.second_brain = SecondBrainClient(str(vault))
    try:
        yield client, vault
    finally:
        siris.second_brain = previous


def test_a_memory_capture_goes_to_the_memory_inbox_not_a_new_note(brain_api) -> None:
    # SirisOS's wrap-up (4.4): the day's line and tomorrow's top 3 are filed onto the notes they're about.
    client, vault = brain_api
    r = client.post("/siris/brain/capture", json={"text": "Planned for 2026-10-25: an 8 km run.", "kind": "memory", "tags": ["wrap-up"]})
    assert r.status_code == 200 and r.json()["title"] == "Siris Memory Inbox" and r.json()["filed_later"] is True
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent == {"title": "", "content": "Planned for 2026-10-25: an 8 km run.", "tags": ["siris-memory", "wrap-up"]}
    assert client.post("/siris/brain/capture", json={"url": "https://x.example", "kind": "memory"}).status_code == 422


def test_capture_lands_in_the_inbox(brain_api) -> None:
    client, vault = brain_api
    r = client.post("/siris/brain/capture", json={"text": "Look into Frigate for the driveway camera. Maybe on the Pi?", "tags": ["Idea"]})
    assert r.status_code == 200 and r.json()["saved"] is True
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["type"] == "capture" and sent["tags"] == ["capture", "idea"]
    assert sent["title"] == "Look into Frigate for the driveway camera. Maybe…"


def test_a_claude_chat_capture_waits_for_the_nightly_distill(brain_api) -> None:
    client, vault = brain_api
    r = client.post("/siris/brain/capture", json={"text": "User: should I get 64 GB?\nClaude: ...", "kind": "chat",
                                                   "title": "Mac mini memory", "url": "https://claude.ai/chat/abc"})
    assert r.status_code == 200
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["title"].startswith("Claude Chat ") and sent["title"].endswith(" – Mac mini memory")
    assert sent["source"] == "Claude chat: https://claude.ai/chat/abc" and "claude-chat" in sent["tags"]
    assert sent["type"] == "capture"


def test_ask_answers_from_notes_and_names_them(brain_api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, vault = brain_api
    siris = get_siris()
    client.post("/siris/brain/capture", json={"text": "Plex runs on the home server's RTX 3060.", "title": "Plex"})
    monkeypatch.setattr(siris, "llm", None)
    r = client.post("/siris/brain/ask", json={"question": "rtx 3060"}).json()
    assert r["answer"].startswith("From your note Plex:") and r["sources"][0] == {"title": "Plex", "path": "Notes/Plex.md", "as_of": "2026-10-02"}

    class LLM:
        prompt = ""

        async def generate(self, messages, **kwargs):
            LLM.prompt = messages[1]["content"]
            return LLMResponse(text="Plex runs on the RTX 3060, as of October 2026 (from: Plex).")

    monkeypatch.setattr(siris, "llm", LLM())
    if siris.model_router is not None:
        monkeypatch.setattr(siris.model_router, "pick", lambda llm, label, role="general": (None, llm))
    r = client.post("/siris/brain/ask", json={"question": "rtx 3060"}).json()
    assert r["answer"].endswith("(from: Plex).") and "Plex (as of 2026-10-02)" in LLM.prompt
    assert client.post("/siris/brain/ask", json={"question": "zebra quilting"}).json() == {
        "answer": "Siris doesn't have anything on that yet.", "sources": []}


def test_review_queue_and_marking_reviewed(brain_api) -> None:
    client, vault = brain_api
    assert client.get("/siris/brain/review?count=1").json()[0]["title"] == "2026-10-02"
    assert client.post("/siris/brain/reviewed", json={"title": "Plex"}).json()["reviewed"] is True
    assert _calls(vault)[-1]["args"] == ["review", "--output", "json", "--", "Plex"]


def test_capture_a_link_keeps_it_as_the_source(brain_api) -> None:
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"url": "https://www.home-assistant.io/integrations/frigate/"})
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["source"] == "https://www.home-assistant.io/integrations/frigate/"
    assert sent["title"] == "home-assistant.io – integrations – frigate"


def test_a_photo_capture_is_saved_in_the_vault_and_embedded(brain_api) -> None:
    import base64

    client, vault = brain_api
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    r = client.post("/siris/brain/capture", json={"text": "Pit lid cracked at Mooroopna PS", "image": data_url})
    assert r.status_code == 200
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["tags"] == ["capture", "photo"]
    embed = sent["content"].split("![[")[1].rstrip("]")
    assert sent["content"].startswith("Pit lid cracked at Mooroopna PS\n\n![[Attachments/")
    assert (vault / embed).read_bytes() == jpeg and embed.endswith(".jpg")
    # A photo on its own is titled by when it was taken; a second one never overwrites the first.
    again = client.post("/siris/brain/capture", json={"image": data_url, "title": "Same"}).json()
    client.post("/siris/brain/capture", json={"image": data_url, "title": "Same"})
    assert again["saved"] is True and len(list((vault / "Attachments").iterdir())) == 3
    alone = client.post("/siris/brain/capture", json={"image": data_url}).json()
    assert json.loads(_calls(vault)[-1]["stdin"])[0]["title"].startswith("Photo ") and alone["saved"]
    assert client.post("/siris/brain/capture", json={"image": "data:text/html;base64,PGI+"}).status_code == 422


def test_capture_needs_something(brain_api) -> None:
    client, _ = brain_api
    assert client.post("/siris/brain/capture", json={}).status_code == 422


def test_capture_title() -> None:
    assert capture_title("# Big idea\nmore", "") == "Big idea"
    assert capture_title("", "") == "Capture"


def test_learned_today_reads_the_activity_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"2026-10-02": "- 🧠 Learned [[Old Way]]"})
    (vault / "activity.json").write_text(json.dumps([
        {"date": "2026-10-02", "line": "🧠 Learned [[Plex]]"},
        {"date": "2026-10-02", "line": "🔗 Linked [[Plex]] ↔ [[Home Server]]"},
        {"date": "2026-10-02", "line": "🧠 Updated [[Plex]]"},
        {"date": "2026-10-03", "line": "🧠 Updated [[Dom Mairs]]"},
    ]))
    client = SecondBrainClient(str(vault))
    assert client.learned_today("2026-10-02") == [{"title": "Plex", "action": "learned"}]
    assert client.learned_today("2026-10-04") == []


def test_map_and_today(brain_api) -> None:
    client, _ = brain_api
    assert client.get("/siris/brain/map.html").text == "<html>map of 1 notes</html>"
    siris = get_siris()
    assert siris.second_brain.learned_today("2026-10-02") == [
        {"title": "Plex", "action": "learned"}, {"title": "Dom Mairs", "action": "updated"},
    ]
    assert siris.second_brain.learned_today("2026-10-03") == []
    assert client.get("/siris/brain/today").json()["items"] == siris.second_brain.learned_today(
        client.get("/siris/brain/today").json()["date"]
    )


def test_insights_route_and_tool(brain_api) -> None:
    client, vault = brain_api
    got = client.get("/siris/brain/insights", params={"days": 14}).json()
    assert got["highlights"] == ["1 notes"] and len(got["activity"]) == 14
    assert _calls(vault)[-1]["args"] == ["insights", "--days", "14", "--output", "json"]
    assert client.get("/siris/brain/insights", params={"days": 3}).status_code == 422
    tool = BrainInsightsTool(get_siris().second_brain)
    assert len(tool.run(days=1000)["activity"]) == 365  # clamped


def test_routes_404_without_a_brain(client: TestClient) -> None:
    siris = get_siris()
    previous, siris.second_brain = siris.second_brain, None
    try:
        assert client.get("/siris/brain/today").status_code == 404
        assert client.get("/siris/brain/insights").status_code == 404
        assert client.post("/siris/brain/capture", json={"text": "x"}).status_code == 404
    finally:
        siris.second_brain = previous


def test_brain_page_is_served(client: TestClient) -> None:
    r = client.get("/brain")
    assert r.status_code == 200 and "/siris/brain/map.html" in r.text


def test_link_and_not_related_routes(brain_api) -> None:
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"title": "Pump Stations", "text": "Wet wells"})
    client.post("/siris/brain/capture", json={"title": "Rising Mains", "text": "Pressure sewers"})

    r = client.post("/siris/brain/link", json={"a": "pump stations", "b": "Rising Mains"})
    assert r.status_code == 200 and r.json()["action"] == "linked"
    assert _calls(vault)[-1]["args"] == ["link", "--output", "json", "--", "pump stations", "Rising Mains"]
    assert "[[Rising Mains]]" in json.loads((vault / "notes.json").read_text())["Pump Stations"]

    r = client.post("/siris/brain/not-related", json={"a": "Pump Stations", "b": "Rising Mains"})
    assert r.json() == {"action": "dismissed", "a": "Pump Stations", "b": "Rising Mains", "changed": ["Pump Stations", "Rising Mains"]}
    assert _calls(vault)[-1]["args"][:4] == ["unrelate", "--output", "json", "--"]

    r = client.post("/siris/brain/link", json={"a": "Pump Stations", "b": "Nope"})
    assert r.status_code == 422 and "No note called 'Nope'" in r.json()["detail"]
    assert client.post("/siris/brain/link", json={"a": "", "b": "x"}).status_code == 422


def test_done_route_ticks_a_task_or_finishes_a_project(brain_api) -> None:
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"title": "Melbourne 10 km", "text": "Race day"})
    r = client.post("/siris/brain/done", json={"note": "melbourne 10 km", "task": "Race day"})
    assert r.status_code == 200 and r.json() == {"action": "done", "note": "Melbourne 10 km", "task": "Race day"}
    assert _calls(vault)[-1]["args"] == ["done", "--output", "json", "--", "melbourne 10 km", "Race day"]
    r = client.post("/siris/brain/done", json={"note": "Melbourne 10 km"})
    assert r.json()["action"] == "project-done" and _calls(vault)[-1]["args"] == ["done", "--output", "json", "--", "Melbourne 10 km"]
    assert client.post("/siris/brain/done", json={"note": "Nope"}).status_code == 422
    assert client.post("/siris/brain/done", json={"note": ""}).status_code == 422


def test_unlink_autolink_and_note_routes(brain_api) -> None:
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"title": "Pump Stations", "text": "Wet wells"})
    client.post("/siris/brain/capture", json={"title": "Rising Mains", "text": "Pressure sewers"})
    r = client.post("/siris/brain/unlink", json={"a": "pump stations", "b": "Rising Mains"})
    assert r.status_code == 200 and r.json()["action"] == "unlinked"
    assert _calls(vault)[-1]["args"] == ["unlink", "--output", "json", "--", "pump stations", "Rising Mains"]
    assert client.post("/siris/brain/unlink", json={"a": "Pump Stations", "b": "Nope"}).status_code == 422

    r = client.post("/siris/brain/autolink")
    assert r.status_code == 200 and r.json()["linked"][0]["confidence"] == 88
    assert _calls(vault)[-1]["args"] == ["autolink", "--output", "json"]

    note = client.get("/siris/brain/note", params={"title": "Pump Stations"})
    assert note.status_code == 200 and note.json()["title"] == "Pump Stations" and "backlinks" in note.json()
    assert client.get("/siris/brain/note", params={"title": "Nope"}).status_code == 404


def test_link_commits_when_syncing(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {"A Note": "a", "B Note": "b"})
    seen = []
    client = SecondBrainClient(str(vault), git_sync=True)
    client._commit = seen.append  # type: ignore[method-assign]
    client._schedule_push = lambda: None  # type: ignore[method-assign]
    client._maybe_pull = lambda: None  # type: ignore[method-assign]
    client.link("A Note", "B Note")
    client.unrelate("A Note", "B Note")
    assert seen == ["siris: linked A Note and B Note", "siris: A Note and B Note aren't related"]


def test_delete_restore_purge_and_deleted_routes(brain_api) -> None:
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"title": "Old Router", "text": "Netgear"})
    client.post("/siris/brain/capture", json={"title": "Spare Pi", "text": "Pi 4"})

    r = client.post("/siris/brain/delete", json={"title": "old router"})
    assert r.status_code == 200 and r.json()["title"] == "Old Router" and r.json()["deleted_to"].startswith(".deleted/")
    assert _calls(vault)[-1]["args"] == ["delete", "--output", "json", "--", "old router"]
    assert [d["title"] for d in client.get("/siris/brain/deleted").json()] == ["Old Router"]

    r = client.post("/siris/brain/restore", json={"title": "Old Router"})
    assert r.json()["action"] == "restored" and client.get("/siris/brain/deleted").json() == []

    r = client.post("/siris/brain/delete", json={"title": "Spare Pi", "permanent": True})
    assert r.json()["permanent"] is True and r.json()["deleted_to"] is None
    assert _calls(vault)[-1]["args"] == ["delete", "--output", "json", "--permanent", "--", "Spare Pi"]

    client.post("/siris/brain/delete", json={"title": "Old Router"})
    assert client.post("/siris/brain/purge", json={"title": "Old Router"}).json()["action"] == "purged"


def test_delete_errors_are_422(brain_api) -> None:
    client, _ = brain_api
    r = client.post("/siris/brain/delete", json={"title": "Nope"})
    assert r.status_code == 422 and "No note called 'Nope'" in r.json()["detail"]
    assert client.post("/siris/brain/restore", json={"title": "Nope"}).status_code == 422
    assert client.post("/siris/brain/delete", json={"title": ""}).status_code == 422


def test_delete_tool_needs_confirmation_and_restore_does_not(tmp_path: Path) -> None:
    from app.plugins.base import ToolPermission
    from app.plugins.builtin.second_brain import BrainDeleteNoteTool, BrainRestoreNoteTool

    vault = _vault(tmp_path, {"Old Router": "Netgear"})
    client = SecondBrainClient(str(vault))
    delete, restore = BrainDeleteNoteTool(client), BrainRestoreNoteTool(client)
    assert delete.permission is ToolPermission.CONFIRM_REQUIRED
    assert restore.permission is ToolPermission.SAFE_ACTION
    assert delete.run(title="Old Router")["action"] == "deleted"
    assert restore.run(title="Old Router")["action"] == "restored"
    assert "No note called" in delete.run(title="Nope")["error"]


def test_delete_tools_registered_with_a_brain(tmp_path: Path) -> None:
    siris = SirisAI(settings=Settings(brain_path=str(_vault(tmp_path, {}))))
    assert {"brain_delete_note", "brain_restore_note"} <= {t.name for t in siris.tools.list()}


def test_tasks_route_lists_open_tasks(brain_api) -> None:
    client, vault = brain_api
    r = client.get("/siris/brain/tasks")
    assert r.status_code == 200 and r.json() == {"tasks": [{"task": "Send levels to Dana", "note": "Werribee Drainage", "due": "2026-10-20", "type": "project"}]}
    assert _calls(vault)[-1]["args"] == ["tasks", "--output", "json"]


def test_projects_route_lists_project_notes(brain_api) -> None:
    client, _ = brain_api
    assert client.get("/siris/brain/projects").json() == {"projects": [
        {"title": "Akaroa Upgrade", "status": "done", "aliases": [], "path": "01-Projects/Akaroa Upgrade.md"},
        {"title": "Werribee Drainage", "status": "active", "aliases": ["WD"], "path": "01-Projects/Werribee Drainage.md"},
    ]}


def test_merge_distinct_duplicates_and_project_pages(tmp_path: Path) -> None:
    from app.plugins.builtin.second_brain import BrainMergeNotesTool, BrainNotesDistinctTool

    vault = _vault(tmp_path / "vault", {"GVW Project Closure": "# GVW Project Closure", "GVW Contract Closure": "# GVW Contract Closure"})
    client = SecondBrainClient(str(vault))
    assert client.duplicates()[0]["b"] == "GVW Contract Closure"
    merged = BrainMergeNotesTool(client).run(source="gvw project closure", into="GVW Contract Closure")
    assert merged == {"action": "merged", "from": "GVW Project Closure", "into": "GVW Contract Closure"}
    assert _calls(vault)[-1]["args"] == ["merge", "--output", "json", "--", "gvw project closure", "GVW Contract Closure"]
    assert "error" in BrainMergeNotesTool(client).run(source="Nope", into="GVW Contract Closure")
    assert BrainNotesDistinctTool(client).run(a="GVW Project Closure", b="GVW Contract Closure")["action"] == "distinct"
    assert client.project_pages()[0]["project"] == "Melrose East Pump Station"
    assert _calls(vault)[-1]["args"] == ["project-pages", "--apply", "--output", "json"]
    client.file_note("I’ve decided…", "note", new_title="Where I Capture Things")
    assert _calls(vault)[-1]["args"][-4:] == ["--title", "Where I Capture Things", "--", "I’ve decided…"]


def test_add_line(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault", {"CPEng Chartership": "# CPEng"})
    assert SecondBrainClient(str(vault)).add_line("CPEng Chartership", "## Evidence from your notes", "- [[X]]: 14")["action"] == "added"
    assert _calls(vault)[-1]["args"] == ["add-line", "--output", "json", "--", "CPEng Chartership", "## Evidence from your notes", "- [[X]]: 14"]



def test_edit_route_replaces_a_notes_text_through_the_engine(brain_api) -> None:
    """4.4: Brad edits a note from SirisOS; an edit made from an older copy is refused."""
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"title": "Gym Plan", "text": "Three days"})
    r = client.post("/siris/brain/edit", json={"title": "gym plan", "body": "Four days a week.", "version": "v1"})
    assert r.status_code == 200 and r.json()["action"] == "edited" and r.json()["version"] == "v2"
    call = _calls(vault)[-1]
    assert call["args"] == ["edit", "--output", "json", "--body", "-", "--expect", "v1", "--", "gym plan"] and call["stdin"] == "Four days a week."
    assert client.get("/siris/brain/note", params={"title": "Gym Plan"}).json()["body"] == "Four days a week."
    stale = client.post("/siris/brain/edit", json={"title": "Gym Plan", "body": "Mine", "version": "old"})
    assert stale.status_code == 422 and "changed since you opened it" in stale.json()["detail"]


def test_set_block_and_past_projects(tmp_path: Path) -> None:
    from app.plugins.builtin.second_brain import BrainPastProjectsTool

    vault = _vault(tmp_path / "vault", {"Culverts": "# Culverts"})
    client = SecondBrainClient(str(vault))
    assert client.set_block("Culverts", "topic-summary", "- Scour at outlets")["action"] == "set"
    assert _calls(vault)[-1]["args"] == ["set-block", "--output", "json", "--", "Culverts", "topic-summary", "-"]
    assert _calls(vault)[-1]["stdin"] == "- Scour at outlets"

    class FakeSearch:
        def search(self, query: str, limit: int = 10) -> list[dict]:
            return [{"title": "Dellows Underslip", "path": "Work/WSP/Projects/Dellows Underslip.md"},
                    {"title": "Home Server", "path": "Notes/Home Server.md"},
                    {"title": "Culverts", "path": "Work/Topics/Culverts.md"},
                    {"title": "Dellows Underslip – Outlet Options", "path": "Work/WSP/Dellows Underslip – Outlet Options.md"}]

    out = BrainPastProjectsTool(FakeSearch()).run(query="culvert outlet")
    assert [p["title"] for p in out["pages"]] == ["Dellows Underslip", "Culverts"]
    assert [n["title"] for n in out["notes"]] == ["Dellows Underslip – Outlet Options"]   # never personal notes



def test_recent_notes_route(brain_api) -> None:
    """4.8: the most recently updated notes under a folder, for SirisOS's Work page."""
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"title": "Pump Stations", "text": "Wet wells"})
    got = client.get("/siris/brain/recent", params={"folder": "Work", "limit": 5}).json()
    assert got and all(n["as_of"] == "2026-11-06" and n["path"].startswith("Work/") for n in got)
    assert _calls(vault)[-1]["args"] == ["recent", "--folder", "Work", "--limit", "5"]


def test_handwriting_review_routes(brain_api, tmp_path: Path) -> None:
    from app.onenote import importer, pages
    from app.onenote.convert import render_strokes

    client, vault = brain_api
    r = client.get("/siris/brain/handwriting")
    assert r.status_code == 200 and r.json()[0]["odd_words"] == ["meroh"]
    r = client.post("/siris/brain/handwriting/check", json={"title": "Site Visit", "text": "Dellows underslip near Moutere"})
    assert r.json()["action"] == "corrected"
    assert _calls(vault)[-1]["args"] == ["handwriting-check", "--output", "json", "--text", "-", "--", "Site Visit"]
    assert _calls(vault)[-1]["stdin"] == "Dellows underslip near Moutere"
    assert client.post("/siris/brain/handwriting/check", json={"title": "Site Visit"}).json()["action"] == "checked"

    siris = get_siris()
    siris.settings.onenote_token_path = str(tmp_path / "onenote" / "token.json")
    siris.state.set(importer.STATE_KEY, {"page-1": {"title": "Site Visit", "modified": "2026-10-01"}})
    ink = '<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML"><inkml:trace>0 0, 400 0, 400 300</inkml:trace></inkml:ink>'

    class FakeOneNote:
        calls = 0

        def page_content(self, pid: str):
            FakeOneNote.calls += 1
            assert pid == "page-1"
            return "<p></p>", ink

        def close(self) -> None:
            pass

    images = pages.page_images(siris, ["Site Visit"], client_factory=FakeOneNote)
    assert images == render_strokes([[(0, 0), (400, 0), (400, 300)]]) and images[0].startswith(b"\x89PNG")
    assert pages.page_images(siris, ["Old Title", "site visit"], client_factory=FakeOneNote) == images
    assert FakeOneNote.calls == 1   # drawn once, then kept
    r = client.get("/siris/brain/handwriting/image", params={"title": "Site Visit", "n": 1})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.headers["x-page-parts"] == "1"
    assert client.get("/siris/brain/handwriting/image", params={"title": "Unknown page"}).status_code == 404
