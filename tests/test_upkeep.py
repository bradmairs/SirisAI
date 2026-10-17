"""Upkeep (app/upkeep/): backups and the restore drill, housekeeping, GitHub
issues, self-update (SirisAI's side), and eval-gated model trials."""

import gzip
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.core import daily_jobs
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.db.models import AttentionItem, MetricSample, ToolExecution
from app.db.session import SessionLocal
from app.evals import tool_choice
from app.hub import attention
from app.llm.base import LLMResponse, ToolCall
from app.llm.metering import METRIC_TOTAL_TOKENS
from app.main import app
from app.upkeep import backups, housekeeping, issues, models, updates


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        session.query(AttentionItem).delete()
        session.commit()
    siris = SirisAI(settings=Settings())
    for key in (models.OVERRIDE_KEY, models.TRIALS_KEY, updates.SEEN_KEY, issues.STATE_KEY, housekeeping.HISTORY_KEY):
        siris.state.delete(key)
    siris.pending_notifications.clear()
    yield


def _siris(tmp_path: Path, **settings: Any) -> SirisAI:
    defaults = {"timezone": "UTC", "backup_dir": str(tmp_path / "backups"), "update_dir": str(tmp_path / "update")}
    return SirisAI(settings=Settings(**{**defaults, **settings}))


def _items(siris: SirisAI, source: str) -> list[dict[str, Any]]:
    return [i for i in siris.attention.list() if i["source"] == source]


# -- backups and the restore drill -------------------------------------------------


def test_a_backup_is_a_consistent_gzipped_copy_and_only_the_newest_are_kept(tmp_path: Path) -> None:
    siris = _siris(tmp_path, backup_keep=2, backup_copy_to=str(tmp_path / "nas"))
    siris.memory.remember("backup canary: the spare key is under the frog")
    start = datetime(2026, 6, 1, 2, 0, tzinfo=UTC)
    results = [backups.make_backup(siris, start + timedelta(days=n)) for n in range(3)]
    assert results[2]["removed"] == ["sirisai-20260601-020000.db.gz"]
    kept = backups.list_backups(siris.settings)
    assert [b["name"] for b in kept] == ["sirisai-20260603-020000.db.gz", "sirisai-20260602-020000.db.gz"]
    assert sorted(p.name for p in (tmp_path / "nas").iterdir()) == [b["name"] for b in reversed(kept)]

    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(Path(kept[0]["path"]).read_bytes()))
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE content LIKE 'backup canary%'").fetchone()[0] >= 1
    conn.close()


def test_the_restore_drill_proves_a_backup_opens_and_says_so(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    backups.make_backup(siris)
    report = backups.restore_drill(siris)
    assert report["passed"] and report["integrity"] == "ok" and report["tables"] > 20
    assert isinstance(report["memories_readable"], int)
    item = _items(siris, "upkeep")[0]
    assert item["title"] == "Restore drill passed" and item["severity"] == "info"


def test_a_broken_backup_fails_the_drill_loudly(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "sirisai-20990101-000000.db.gz").write_bytes(gzip.compress(b"this is not a database"))
    report = backups.restore_drill(siris)
    assert not report["passed"]
    assert "won't open" in report["problems"][0] or "integrity" in report["problems"][0]
    item = _items(siris, "upkeep")[0]
    assert item["title"] == "Restore drill failed" and item["severity"] == "urgent"
    assert siris.pending_notifications.list_pending() == []  # no push channel: the feed item is the message


def test_no_backups_at_all_fails_the_drill(tmp_path: Path) -> None:
    report = backups.restore_drill(_siris(tmp_path), notify=False)
    assert report["problems"] == ["there are no backups to restore"]


def test_stale_backups_are_flagged(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    assert backups.freshness(siris) == "There are no backups of SirisAI's database yet."
    backups.make_backup(siris)
    assert backups.freshness(siris) is None
    assert "3 days old" in backups.freshness(siris, datetime.now(UTC) + timedelta(days=3))
    assert backups.freshness(_siris(tmp_path, backup_hour_local=None)) is None


# -- housekeeping --------------------------------------------------------------------


def test_housekeeping_prunes_old_rows_and_keeps_recent_ones(tmp_path: Path) -> None:
    siris = _siris(tmp_path, audit_keep_days=30)
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=45)
    with SessionLocal() as session:
        session.add(ToolExecution(tool_name="old_tool_hk", arguments="{}", duration_ms=1, created_at=old))
        session.add(MetricSample(metric_name="hk_metric", value=1, created_at=old - timedelta(days=60)))
        session.commit()
    siris.audit.record("new_tool_hk", {})
    pruned = housekeeping.prune(siris)
    assert pruned["audit"] >= 1 and pruned["metrics"] >= 1
    assert siris.audit.find_recent("old_tool_hk") == [] and siris.audit.find_recent("new_tool_hk")


def test_housekeeping_measures_and_warns_about_disk_and_tokens(tmp_path: Path) -> None:
    siris = _siris(tmp_path, disk_warn_percent=101, token_budget_daily=1000)
    siris.metrics.record_sample(METRIC_TOTAL_TOKENS, 5000)
    report = housekeeping.run(siris)
    assert report["tokens_24h"] >= 5000 and report["db_bytes"] > 0 and report["disk_free_percent"] > 0
    titles = {i["title"]: i for i in _items(siris, "upkeep")}
    assert "Disk nearly full" in titles and "Over the token budget" in titles
    assert "Backups are out of date" in titles  # none made yet
    assert siris.state.get(housekeeping.HISTORY_KEY)[-1]["tokens_24h"] == report["tokens_24h"]

    backups.make_backup(siris)
    calm = _siris(tmp_path, disk_warn_percent=0)
    housekeeping.run(calm)
    assert not any(i["key"] in ("upkeep:disk", "upkeep:backup-stale") for i in calm.attention.list())


# -- GitHub issues -----------------------------------------------------------------------


class FakeGitHub:
    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.issues = existing or []
        self.calls: list[tuple[str, str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if "/issues" in request.url.path:
            assert request.headers["Authorization"] == "Bearer ghp_test"
        if request.method == "GET" and request.url.path.endswith("/issues"):
            return httpx.Response(200, json=self.issues)
        if request.method == "POST" and request.url.path.endswith("/issues"):
            issue = {"number": 42, "title": body["title"], "html_url": "https://github.com/o/r/issues/42"}
            self.issues.append(issue)
            return httpx.Response(201, json=issue)
        if request.method == "POST" and request.url.path.endswith("/comments"):
            return httpx.Response(201, json={})
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": "v9.9", "body": "- Big things", "html_url": "https://x/rel"})
        return httpx.Response(404)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    real = httpx.Client

    def client(*args: Any, **kwargs: Any) -> httpx.Client:
        return real(*args, **{**kwargs, "transport": httpx.MockTransport(fake.handler)})

    monkeypatch.setattr(issues.httpx, "Client", client)
    return fake


def test_secrets_are_blanked_before_anything_is_sent() -> None:
    assert issues.redact({"api_key": "abc", "nested": {"password": "p", "room": "kitchen"}}) == {"api_key": "[redacted]", "nested": {"password": "[redacted]", "room": "kitchen"}}
    assert issues.redact("failed with Bearer abcdefghijklmnop and ghp_" + "a" * 30) == "failed with Bearer [redacted] and [redacted]"


def test_nothing_is_proposed_until_a_repo_and_token_are_set(tmp_path: Path) -> None:
    assert issues.propose(_siris(tmp_path), "x", "y", "f") is None


def test_an_issue_is_asked_about_first_then_filed_and_repeats_become_comments(tmp_path: Path, github: FakeGitHub) -> None:
    siris = _siris(tmp_path, github_issues_repo="o/r", github_token="ghp_test")
    siris.audit.record("docker_list_containers", {"token": "s3cret"}, error="connection refused")
    issues.propose(siris, "'docker_list_containers' keeps failing", "Paused by the circuit breaker.", "tool:docker")
    item = _items(siris, "issues")[0]
    assert item["title"] == "File a GitHub issue? 'docker_list_containers' keeps failing" and github.calls == []

    attention.act(siris, item["id"], "file")
    method, path, body = github.calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues") and body["labels"] == ["filed-by-sirisai"]
    assert body["title"] == "[SirisAI] 'docker_list_containers' keeps failing"
    assert f"SirisAI version: {__version__}" in body["body"] and "connection refused" in body["body"] and "s3cret" not in body["body"]

    again = issues.file(siris, "'docker_list_containers' keeps failing", "Again.", "tool:docker")
    assert again["action"] == "skipped (commented today)"
    filed = siris.state.get(issues.STATE_KEY)
    filed["tool:docker"]["at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    siris.state.set(issues.STATE_KEY, filed)
    assert issues.file(siris, "'docker_list_containers' keeps failing", "Again.", "tool:docker")["action"] == "commented"
    assert github.calls[-1][1] == "/repos/o/r/issues/42/comments"


def test_auto_mode_files_straight_away(tmp_path: Path, github: FakeGitHub) -> None:
    siris = _siris(tmp_path, github_issues_repo="o/r", github_token="ghp_test", github_issues_mode="auto")
    assert issues.propose(siris, "Restore drill failed", "Backups won't open.", "upkeep:restore-drill")["action"] == "filed"
    assert _items(siris, "issues") == []


# -- self-update, SirisAI's side ---------------------------------------------------------


def test_versions_compare_as_decimals() -> None:
    assert updates.is_newer("1.1", "1.09") and updates.is_newer("v2.0", "1.99")
    assert not updates.is_newer("1.1", "1.1") and not updates.is_newer("junk", "1.0")


def test_a_new_release_is_offered_and_update_now_asks_the_updater(tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris(tmp_path)
    assert updates.check(siris)["asked"]
    item = _items(siris, "updates")[0]
    assert item["title"] == "SirisAI 9.9 is out" and "Big things" in item["body"]
    attention.act(siris, item["id"], "apply")
    request = json.loads((tmp_path / "update" / "request.json").read_text())
    assert request["version"] == "9.9" and request["from_version"] == __version__
    assert request["baseline_selection_accuracy"] == tool_choice.selection_report(siris)["accuracy"]
    assert updates.request(siris, "9.9")["requested"] is False  # one at a time
    assert updates.status(siris)["updater_missing"] is False
    assert updates.status(siris, datetime.now(UTC) + timedelta(minutes=16))["updater_missing"] is True
    updates.report(siris, datetime.now(UTC) + timedelta(minutes=16))
    assert any(i["title"] == "The updater isn't running" for i in _items(siris, "upkeep"))


def test_auto_mode_requests_the_update_itself_and_off_never_checks(tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    assert updates.check(_siris(tmp_path, update_mode="auto"))["requested"] is True
    assert updates.check(_siris(tmp_path / "x", update_mode="off")) == {"skipped": "SIRISAI_UPDATE_MODE=off"}
    with pytest.raises(ValueError):
        updates.request(_siris(tmp_path / "y"), "0.5")


def _status(tmp_path: Path, **fields: Any) -> None:
    folder = tmp_path / "update"
    folder.mkdir(exist_ok=True)
    (folder / "status.json").write_text(json.dumps({"version": "9.9", "from_version": __version__, "finished_at": datetime.now(UTC).isoformat(), **fields}))


def test_a_successful_update_is_reported_once(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    _status(tmp_path, state="updated", detail="tool selection 100.0%")
    assert updates.report(siris)["state"] == "updated"
    assert updates.report(siris) is None
    item = _items(siris, "upkeep")[0]
    assert item["title"] == "Updated to 9.9" and item["severity"] == "info"


class FakeNotifyClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: str, **kwargs: Any) -> None:
        self.sent.append({"message": message, **kwargs})


def test_a_rolled_back_update_is_pushed_with_its_log(tmp_path: Path) -> None:
    from app.plugins.builtin.notify import NotifyTool

    siris = _siris(tmp_path)
    phone = FakeNotifyClient()
    siris.tools._tools["notify"] = NotifyTool(phone)  # type: ignore[arg-type]
    _status(tmp_path, state="rolled_back", detail="SirisAI 9.9 didn't come up healthy within 180s; rolled back to 1.2", log_tail="Traceback: boom")
    updates.report(siris)
    item = _items(siris, "upkeep")[0]
    assert item["title"] == "Update to 9.9 rolled back" and "Traceback: boom" in item["body"]
    assert phone.sent[0]["title"] == "SirisAI: Update to 9.9 rolled back" and phone.sent[0]["priority"] == "high"
    assert len(_items(siris, "upkeep")) == 1  # the push didn't add a second copy
    assert siris.outcomes.counts(key="update:9.9")["undone"] >= 1


# -- eval-gated model trials -------------------------------------------------------------


class ChoosingLLM:
    """Picks the right tool for every eval case, or always the same one."""

    def __init__(self, perfect: bool) -> None:
        self.perfect = perfect
        self.answers = {c["prompt"]: c["expect"][0] for c in tool_choice.load_cases()}

    async def generate(self, messages: Any, tools: Any = None) -> LLMResponse:
        prompt = messages[-1]["content"]
        return LLMResponse(tool_calls=[ToolCall(name=self.answers.get(prompt, "echo") if self.perfect else "echo")])

    async def aclose(self) -> None:
        pass


def test_a_better_model_is_offered_and_switching_sticks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris(tmp_path, llm_provider="ollama", ollama_model="small:1b")
    siris.llm = ChoosingLLM(perfect=False)
    built: list[str] = []

    def build(s: SirisAI, model: str) -> ChoosingLLM:
        built.append(model)
        return ChoosingLLM(perfect=True)

    monkeypatch.setattr(models, "build", build)
    result = models.trial(siris, "big:14b")
    assert result["recommend_switch"] and result["candidate_accuracy"] > result["current_accuracy"]
    item = _items(siris, "models")[0]
    assert item["title"].startswith("Switch to big:14b?")
    attention.act(siris, item["id"], "switch")
    assert models.current_model(siris) == "big:14b" and siris.state.get(models.OVERRIDE_KEY) == "big:14b"

    restarted = _siris(tmp_path, llm_provider="ollama", ollama_model="small:1b")
    assert built[-1] == "big:14b" and isinstance(restarted.llm, ChoosingLLM)  # apply_override at startup
    assert models.switch(restarted, None) == {"previous": "big:14b", "now": "small:1b"}


def test_a_worse_model_is_reported_and_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris(tmp_path, llm_provider="ollama", ollama_model="good:8b", candidate_models="bad:1b, good:8b")
    siris.llm = ChoosingLLM(perfect=True)
    monkeypatch.setattr(models, "build", lambda s, m: ChoosingLLM(perfect=False))
    report = models.weekly(siris)
    assert [t["model"] for t in report["trials"]] == ["bad:1b"]  # the current one isn't trialled
    assert not report["trials"][0]["recommend_switch"]
    assert _items(siris, "models")[0]["title"].startswith("Kept good:8b")
    assert models.weekly(siris)["trials"] == []  # not again for four weeks
    with pytest.raises(ValueError):
        models.trial(_siris(tmp_path), "x")  # needs Ollama


# -- jobs and routes ------------------------------------------------------------------------


def test_the_upkeep_jobs_are_scheduled() -> None:
    names = {j.name for j in daily_jobs.JOBS}
    assert {"backup", "restore_drill", "housekeeping", "update_check", "model_trial"} <= names
    siris = SirisAI(settings=Settings())
    job = {j.name: j for j in daily_jobs.JOBS}
    assert job["model_trial"].hour(siris) is None  # no candidates
    assert job["restore_drill"].monthday == 1


def test_upkeep_routes(client: TestClient, tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        assert client.post("/siris/upkeep/backup").json()["size_bytes"] > 0
        assert client.post("/siris/upkeep/restore-drill").json()["passed"] is True
        assert "pruned" in client.post("/siris/upkeep/housekeeping").json()
        overview = client.get("/siris/upkeep").json()
        assert overview["backups"] and overview["housekeeping"] and overview["update"]["running"] == __version__
        assert client.post("/siris/upkeep/update/apply", json={"version": "0.1"}).status_code == 409
        assert client.post("/siris/upkeep/models/trial", json={}).status_code == 422
        assert siris.run_tool("upkeep_status", {})["newest_backups"]
    finally:
        app.dependency_overrides.pop(get_siris, None)
