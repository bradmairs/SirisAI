import json
import shutil
import uuid
import subprocess
import textwrap
from pathlib import Path

import pytest

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
    stdin = sys.stdin.read() if "--json" in args else ""
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
    if cmd == "relink":
        print("Notes/A.md: + B\\nAdded 3 links.")
        sys.exit(0)
    if cmd == "search":
        q = args[args.index("--") + 1].lower()
        hits = [{"title": t, "path": f"Notes/{t}.md", "type": "note", "tags": [], "score": 5, "excerpts": [b[:80]]}
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
        "path": "People/Dom Mairs.md", "type": "person", "areas": [], "reasons": ["fake"],
    }
    new = tool.dry_run(title="Knee Soreness", content="sore after runs")
    assert new["would_create"] is True and new["areas"] == ["Health"]
    assert _calls(vault)[-1]["args"] == ["route", "--type", "auto", "--output", "json", "-c", "sore after runs", "--", "Knee Soreness"]


def test_learn_tool_leaves_the_type_to_the_router_by_default(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    BrainLearnTool(SecondBrainClient(str(vault))).run(title="Knee Soreness", content="sore after runs")
    assert "type" not in json.loads(_calls(vault)[-1]["stdin"])[0]


def test_learn_tool_only_passes_known_types(tmp_path: Path) -> None:
    vault = _vault(tmp_path, {})
    BrainLearnTool(SecondBrainClient(str(vault))).run(title="X", content="y", type="spaceship")
    assert "type" not in json.loads(_calls(vault)[-1]["stdin"])[0]


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
    missing = [r for r in check_integrations(Settings(brain_path=str(tmp_path))) if r["name"] == "Second Brain"]
    assert missing == [{"name": "Second Brain", "ok": False, "detail": f"no tools/brain.py under {tmp_path}", "ms": None}]
    present = [r for r in check_integrations(Settings(brain_path=str(_vault(tmp_path)))) if r["name"] == "Second Brain"]
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
    health = [r for r in check_integrations(Settings(brain_path=str(vault)), second_brain=client) if r["name"] == "Second Brain"]
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
        {"title": "Melbourne Marathon 2026 10 km", "score": 6, "excerpts": ["Target under 1:10:00"]},
        {"title": "Home", "score": 1, "excerpts": ["one weak word"]},
    ])
    text = siris.context.build(siris, "how's my 10 km prep going?")
    assert "second brain" in text
    assert "[Melbourne Marathon 2026 10 km] Target under 1:10:00" in text
    assert "[Home]" not in text
    assert siris.second_brain.queries == [("how's my 10 km prep going?", 3)]


@needs_db
def test_context_brain_notes_can_be_turned_off() -> None:
    siris = SirisAI(settings=Settings(brain_context_notes=0))
    siris.second_brain = _FakeBrain([{"title": "X", "score": 9, "excerpts": ["y"]}])
    assert "second brain" not in siris.context.build(siris, "anything")
    assert siris.second_brain.queries == []


@needs_db
def test_a_broken_vault_never_breaks_chat_context() -> None:
    siris = SirisAI(settings=Settings())
    siris.second_brain = _FakeBrain(fail=True)
    text = siris.context.build(siris, "hello")
    assert text is not None and "second brain" not in text


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


def test_capture_lands_in_the_inbox(brain_api) -> None:
    client, vault = brain_api
    r = client.post("/siris/brain/capture", json={"text": "Look into Frigate for the driveway camera. Maybe on the Pi?", "tags": ["Idea"]})
    assert r.status_code == 200 and r.json()["saved"] is True
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["type"] == "capture" and sent["tags"] == ["capture", "idea"]
    assert sent["title"] == "Look into Frigate for the driveway camera. Maybe…"


def test_capture_a_link_keeps_it_as_the_source(brain_api) -> None:
    client, vault = brain_api
    client.post("/siris/brain/capture", json={"url": "https://www.home-assistant.io/integrations/frigate/"})
    sent = json.loads(_calls(vault)[-1]["stdin"])[0]
    assert sent["source"] == "https://www.home-assistant.io/integrations/frigate/"
    assert sent["title"] == "home-assistant.io – integrations – frigate"


def test_capture_needs_something(brain_api) -> None:
    client, _ = brain_api
    assert client.post("/siris/brain/capture", json={}).status_code == 422


def test_capture_title() -> None:
    assert capture_title("# Big idea\nmore", "") == "Big idea"
    assert capture_title("", "") == "Capture"


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
