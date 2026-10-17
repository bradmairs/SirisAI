"""The learning loop (app/learning/), the weekly goal review
(app/goals/review.py) and the once-a-day job runner (app/core/daily_jobs.py)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.anticipation import habits
from app.core import daily_jobs
from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.cortex.orchestrator import Cortex
from app.db.models import AttentionItem, Outcome
from app.db.session import SessionLocal
from app.evals import tool_choice
from app.goals import review as goal_review
from app.hub import attention
from app.learning import feedback, observe, reflection
from app.llm.base import LLMResponse
from app.main import app
from app.plugins.base import ConfirmationRequired, Tool, ToolPermission


class Recorder(Tool):
    description = "fake"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, permission: ToolPermission = ToolPermission.SAFE_ACTION) -> None:
        self.name = name
        self.permission = permission
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return {"ok": True}


class FakeLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[Any] = []

    async def generate(self, messages: Any, tools: Any = None) -> LLMResponse:
        self.prompts.append(messages)
        return LLMResponse(text=self.text)

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean(client: TestClient):
    with SessionLocal() as session:
        session.query(Outcome).delete()
        session.query(AttentionItem).delete()
        session.commit()
    siris = SirisAI(settings=Settings())
    for key in (reflection.LEARNED_CASES_KEY, reflection.HISTORY_KEY, daily_jobs.STATE_KEY, daily_jobs.RESULTS_KEY, habits.STATE_KEY):
        siris.state.delete(key)
    yield


def _siris(**settings: Any) -> SirisAI:
    siris = SirisAI(settings=Settings(timezone="UTC", **settings))
    for name in ("home_assistant_call_service", "home_assistant_get_state", "run_protocol", "undo_last_protocol"):
        siris.tools._tools[name] = Recorder(name, ToolPermission.READ if name.endswith("get_state") else ToolPermission.SAFE_ACTION)
    return siris


def _entity() -> str:
    return f"light.learn_{uuid.uuid4().hex[:8]}"


# -- outcomes from the audit log ------------------------------------------------


def test_an_autonomous_change_reversed_soon_after_is_undone_and_one_left_alone_is_kept() -> None:
    siris = _siris()
    undone, kept = _entity(), _entity()
    siris.run_tool("home_assistant_call_service", {"domain": "light", "service": "turn_on", "entity_id": undone}, trigger="presence_check: someone came home")
    siris.run_tool("home_assistant_call_service", {"domain": "light", "service": "turn_off", "entity_id": undone}, trigger="chat: model chose this tool")
    siris.run_tool("home_assistant_call_service", {"domain": "light", "service": "turn_on", "entity_id": kept}, trigger="routine: 'evening' (at 18:00)")

    mine = lambda found: [f for f in found if f["key"] in (f"light.turn_on:{undone}", f"light.turn_on:{kept}")]  # noqa: E731
    assert mine(observe.undos(siris)) == [{"tool": "home_assistant_call_service", "key": f"light.turn_on:{undone}", "signal": "undone"}]
    later = datetime.now(UTC) + timedelta(minutes=20)
    assert mine(observe.undos(siris, later)) == [{"tool": "home_assistant_call_service", "key": f"light.turn_on:{kept}", "signal": "kept"}]
    assert mine(observe.undos(siris, later)) == []  # each audit entry counts once
    assert siris.outcomes.counts(key=f"light.turn_on:{undone}")["undone"] == 1


def test_undoing_a_protocol_the_scheduler_ran_counts_against_it() -> None:
    siris = _siris()
    name = f"away-{uuid.uuid4().hex[:6]}"
    siris.run_tool("run_protocol", {"name": name}, trigger="presence_check: everyone left")
    siris.run_tool("undo_last_protocol", {}, trigger="chat: model chose this tool")
    observe.undos(siris)
    assert siris.outcomes.counts(key=f"run_protocol:{name}")["undone"] == 1


def test_failed_checks_afterwards_are_recorded_as_unverified() -> None:
    siris = _siris()
    siris.audit.record("docker_restart_container", {"container": "plexy"}, result={}, verified=False, verification_detail="plexy is still exited")
    found = observe.unverified(siris)
    assert {"tool": "docker_restart_container", "why": "plexy is still exited"} in found
    assert observe.unverified(siris) == []


# -- autonomy demotion ------------------------------------------------------------


def test_an_action_undone_again_and_again_waits_for_an_ok_then_earns_its_way_back() -> None:
    siris = _siris()
    entity = _entity()
    args = {"domain": "light", "service": "turn_on", "entity_id": entity}
    key = feedback.action_key("home_assistant_call_service", args)
    for _ in range(3):
        siris.outcomes.record("home_assistant_call_service", "undone", key=key)
    assert feedback.demotion(siris, "home_assistant_call_service", args)
    assert key in feedback.demoted_keys(siris)

    # Asked in chat: runs as normal.
    siris.run_tool("home_assistant_call_service", args, trigger="chat: model chose this tool")
    # On its own: held, with an approval in the attention feed.
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("home_assistant_call_service", args, trigger="presence_check: someone came home")
    assert siris.tools.get("home_assistant_call_service").calls == [args]  # type: ignore[union-attr]
    held = siris.audit.find_recent("home_assistant_call_service", limit=1)[0]
    assert held["confirmation_required"] and "you undid or turned down this 3" in held["policy"]
    item = next(i for i in siris.attention.list() if i["kind"] == "confirm")

    attention.act(siris, item["id"], "approve")
    assert siris.tools.get("home_assistant_call_service").calls == [args, args]  # type: ignore[union-attr]
    assert siris.outcomes.counts(key=key)["accepted"] == 1
    for _ in range(3):
        siris.outcomes.record("home_assistant_call_service", "kept", key=key)
    assert feedback.demotion(siris, "home_assistant_call_service", args) is None


def test_a_routine_asks_for_itself_so_a_held_action_is_raised_once() -> None:
    siris = _siris()
    args = {"domain": "light", "service": "turn_on", "entity_id": _entity()}
    for _ in range(3):
        siris.outcomes.record("home_assistant_call_service", "undone", key=feedback.action_key("home_assistant_call_service", args))
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("home_assistant_call_service", args, trigger="routine: 'evening' (at 18:00)")
    assert [i for i in siris.attention.list() if i["kind"] == "confirm"] == []  # RoutineRunner raises it


def test_suggestions_turned_down_most_of_the_time_stop_buzzing_the_phone() -> None:
    siris = _siris()
    assert not feedback.muted(siris, "attention:habits")
    for n in range(5):
        siris.outcomes.record("attention:habits", "dismissed", ref=f"h{n}")
    assert feedback.muted(siris, "attention:habits")
    siris.outcomes.record("attention:habits", "accepted")
    siris.outcomes.record("attention:habits", "accepted")
    assert not feedback.muted(siris, "attention:habits")  # 2 of 7 accepted is over 20%


def test_answering_a_habit_suggestion_is_counted_once_however_it_was_answered() -> None:
    siris = _siris()
    siris.state.set(habits.STATE_KEY, {"s1": {"id": "s1", "status": "open"}})
    habits.set_status(siris, "s1", "dismissed")
    habits.set_status(siris, "s1", "dismissed")
    assert siris.outcomes.counts(subject="attention:habits")["dismissed"] == 1


def test_items_left_open_for_days_count_as_ignored() -> None:
    siris = _siris()
    siris.attention.raise_item("x:1", "suggestion", "Try this?", source="habits")
    assert observe.ignored_items(siris) == []
    assert observe.ignored_items(siris, datetime.now(UTC) + timedelta(days=4)) == [{"title": "Try this?", "source": "habits"}]
    assert observe.ignored_items(siris, datetime.now(UTC) + timedelta(days=5)) == []


# -- corrections, eval cases, memory proposals ------------------------------------


def _corrected_conversation(siris: SirisAI, request: str) -> str:
    cid = siris.conversation.create_conversation()
    siris.conversation.add_message(cid, "user", request)
    siris.conversation.add_message(cid, "tool", "{}", tool_name="home_assistant_get_state", tool_arguments={})
    siris.conversation.add_message(cid, "assistant", "The big light is off.")
    siris.conversation.add_message(cid, "user", "No, I meant turn it on")
    siris.conversation.add_message(cid, "tool", "{}", tool_name="home_assistant_call_service", tool_arguments={})
    siris.conversation.add_message(cid, "assistant", "Done, it's on.")
    siris.conversation.add_message(cid, "user", "thanks")
    return cid


def test_a_corrected_chat_turn_becomes_an_eval_case() -> None:
    siris = _siris()
    request = f"the big light please {uuid.uuid4().hex[:6]}"
    _corrected_conversation(siris, request)
    report = reflection.reflect(siris)
    assert {"request": request, "wrong_tool": "home_assistant_get_state", "right_tool": "home_assistant_call_service"} in report["corrections"]
    assert {"prompt": request, "expect": ["home_assistant_call_service"], "learned": datetime.now(UTC).date().isoformat(), "instead_of": "home_assistant_get_state"} in report["learned_eval_cases"]
    assert any(c["prompt"] == request for c in tool_choice.all_cases(siris))
    assert reflection.reflect(siris)["learned_eval_cases"] == []  # not twice
    item = next(i for i in siris.attention.list() if i["source"] == "learning")
    assert item["title"] == "What I learned last night" and "corrected me" in item["body"]


def test_plain_follow_ups_are_not_corrections() -> None:
    assert observe.CORRECTION_RE.match("no, the kitchen one")
    assert observe.CORRECTION_RE.match("I meant the bedroom")
    assert observe.CORRECTION_RE.match("That's wrong")
    assert not observe.CORRECTION_RE.match("nothing else thanks")
    assert not observe.CORRECTION_RE.match("now the kitchen")


def test_reflection_proposes_facts_and_remembers_only_on_a_yes() -> None:
    siris = _siris(llm_provider="ollama")
    fact = f"'The big light' means light.living_room_main ({uuid.uuid4().hex[:6]})"
    siris.llm = FakeLLM(f'Sure: [{{"fact": "{fact}", "why": "You corrected me twice."}}]')
    _corrected_conversation(siris, f"big light {uuid.uuid4().hex[:6]}")
    report = reflection.reflect(siris)
    assert report["memory_proposals"] == [fact]
    item = next(i for i in siris.attention.list() if i["title"].startswith("Remember this?"))
    assert [a["id"] for a in item["actions"]] == ["remember", "dismiss"]
    assert not any(m["content"] == fact for m in siris.memory.list_all())
    attention.act(siris, item["id"], "remember")
    assert any(m["content"] == fact for m in siris.memory.list_all())
    assert siris.outcomes.counts(subject="attention:learning")["accepted"] == 1


def test_a_fact_already_remembered_is_not_proposed_again() -> None:
    siris = _siris(llm_provider="ollama")
    fact = f"Brad calls the lounge lamp the reading light {uuid.uuid4().hex[:6]}"
    siris.memory.remember(fact)
    siris.llm = FakeLLM(f'[{{"fact": "{fact}", "why": ""}}]')
    assert reflection.propose_memories(siris, [{"request": "x", "wrong_tool": "a", "correction": "no", "right_tool": None}], []) == []


def test_unparseable_model_output_proposes_nothing() -> None:
    assert reflection._parse_facts("I don't think so") == []
    assert reflection._parse_facts('[{"fact": ""}, {"nope": 1}, {"fact": "A"}]') == [{"fact": "A", "why": ""}]


# -- verify after acting, in chat ---------------------------------------------------


def test_chat_says_so_when_a_change_did_not_check_out() -> None:
    siris = _siris()
    cid = siris.conversation.create_conversation()
    siris.audit.record("home_assistant_call_service", {}, result={}, conversation_id=cid, verified=False, verification_detail="Light.porch is still 'off'")
    assert Cortex._failed_checks(siris, cid, ["home_assistant_call_service"]) == ["light.porch is still 'off'"]
    assert Cortex._failed_checks(siris, "other", ["home_assistant_call_service"]) == []


# -- the weekly goal review ----------------------------------------------------------


def test_the_goal_review_shows_progress_and_ticks_off_the_next_step() -> None:
    siris = _siris()
    for goal in siris.goals.list_goals("active"):
        siris.goals.update_goal_status(goal["id"], "completed")
    goal = siris.goals.create_goal("Get chartered")
    first = siris.goals.add_objective(goal["id"], "Write the engineering summary")["objectives"][0]
    siris.goals.add_objective(goal["id"], "Book the interview")
    bare = siris.goals.create_goal("Renovate the bathroom")

    reviewed = goal_review.review(siris)
    assert {"goal": "Get chartered", "done": 0, "steps": 2, "next": "Write the engineering summary", "idle_days": 0} in reviewed
    items = {i["title"]: i for i in siris.attention.list() if i["source"] == "goals"}
    assert items["Get chartered: 0 of 2 done"]["body"].startswith("Next: Write the engineering summary")
    assert "Renovate the bathroom: no steps yet" in items

    attention.act(siris, items["Get chartered: 0 of 2 done"]["id"], "complete")
    assert any(o["id"] == first["id"] and o["done"] for g in siris.goals.list_goals("active") for o in g["objectives"])
    goal_review.review(siris)
    assert any(i["title"] == "Get chartered: 1 of 2 done" for i in siris.attention.list())

    siris.goals.update_goal_status(bare["id"], "abandoned")
    goal_review.review(siris)
    assert not any(i["title"].startswith("Renovate") for i in siris.attention.list())


# -- the once-a-day job runner ---------------------------------------------------------


def test_daily_jobs_run_once_a_day_at_their_hour_and_one_failure_does_not_stop_the_rest() -> None:
    siris = _siris()
    ran: list[str] = []

    def boom(s: SirisAI, now: datetime) -> None:
        raise RuntimeError("nope")

    jobs = [
        daily_jobs.DailyJob("early", lambda s: 2, lambda s, now: ran.append("early") or {"ok": True}),
        daily_jobs.DailyJob("broken", lambda s: 2, boom),
        daily_jobs.DailyJob("off", lambda s: None, lambda s, now: ran.append("off")),
        daily_jobs.DailyJob("tuesdays", lambda s: 2, lambda s, now: ran.append("tuesdays"), weekday=lambda s: 1),
        daily_jobs.DailyJob("firsts", lambda s: 2, lambda s, now: ran.append("firsts"), monthday=1),
    ]
    monday_1am = datetime(2026, 6, 1, 1, 0, tzinfo=UTC)  # a Monday, the 1st
    assert daily_jobs.run_due(siris, monday_1am, jobs) == {}
    results = daily_jobs.run_due(siris, monday_1am + timedelta(hours=2), jobs)
    assert ran == ["early", "firsts"] and results["broken"]["error"] == "RuntimeError: nope"
    assert daily_jobs.run_due(siris, monday_1am + timedelta(hours=5), jobs) == {}
    daily_jobs.run_due(siris, monday_1am + timedelta(days=1, hours=2), jobs)
    assert ran == ["early", "firsts", "early", "tuesdays"]


def test_the_built_in_jobs_are_reflection_and_the_goal_review() -> None:
    siris = _siris()
    assert {j.name for j in daily_jobs.JOBS} >= {"reflection", "goal_review"}
    assert siris.settings.reflection_hour_local == 3 and siris.settings.goal_review_day == 0


# -- over HTTP ---------------------------------------------------------------------


def test_learning_routes(client: TestClient) -> None:
    siris = _siris()
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        request = f"lights on {uuid.uuid4().hex[:6]}"
        _corrected_conversation(siris, request)
        # Earlier tests' conversations are in the shared database too.
        assert any(c["request"] == request for c in client.post("/siris/learning/reflect").json()["corrections"])
        overview = client.get("/siris/learning").json()
        learned = overview["learned_eval_cases"]
        assert learned >= 1 and any(s["subject"] == "home_assistant_get_state" for s in overview["summary"])
        assert client.get("/siris/learning/outcomes?signal=corrected").json()[0]["subject"] == "home_assistant_get_state"
        assert f'{{"prompt": "{request}", "expect": ["home_assistant_call_service"]}}' in client.get("/siris/learning/eval-cases?format=jsonl").text
        assert client.get("/siris/learning/reflections").json()[0]["corrections"]
        assert "reflection" in client.get("/siris/learning/jobs").json()
        siris.goals.create_goal(f"Learn to weld {uuid.uuid4().hex[:6]}")
        assert client.post("/siris/learning/jobs/goal_review/run").json()["result"]
        assert client.get("/siris/learning/jobs").json()["goal_review"]["last_result"]["result"]
        assert client.post("/siris/learning/jobs/nope/run").status_code == 404
        assert client.delete("/siris/learning/eval-cases").json() == {"cleared": learned}
        assert siris.run_tool("what_have_you_learned", {})["learned_eval_cases"] == 0
    finally:
        app.dependency_overrides.pop(get_siris, None)
