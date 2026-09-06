import uuid

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris


def _fresh_siris() -> SirisAI:
    return SirisAI(settings=get_siris().settings)


# -- MemoryStore usage tracking


def test_remember_starts_with_zero_recall_count() -> None:
    siris = _fresh_siris()
    entry = siris.memory.remember(f"fact {uuid.uuid4()}")
    assert entry["recall_count"] == 0
    assert entry["last_recalled_at"] is None


def test_recall_bumps_usage_on_matching_entries_only() -> None:
    """Distinct markers on each entry, not a shared one -- recall() now
    matches on any significant word in the query (see app/memory/service.py),
    not just an exact whole-string match, so a marker shared between both
    entries would make both "match" via that shared token and no longer
    prove this test's real point: usage bumps only for entries the query
    actually matches."""
    siris = _fresh_siris()
    matched_marker = str(uuid.uuid4())
    unmatched_marker = str(uuid.uuid4())
    matched = siris.memory.remember(f"apple {matched_marker}")
    unmatched = siris.memory.remember(f"banana {unmatched_marker}")

    siris.memory.recall(query=f"apple {matched_marker}")

    matched_after = next(m for m in siris.memory.list_all() if m["id"] == matched["id"])
    unmatched_after = next(m for m in siris.memory.list_all() if m["id"] == unmatched["id"])
    assert matched_after["recall_count"] == 1
    assert matched_after["last_recalled_at"] is not None
    assert unmatched_after["recall_count"] == 0


def test_list_all_does_not_count_as_a_use() -> None:
    siris = _fresh_siris()
    entry = siris.memory.remember(f"fact {uuid.uuid4()}")
    siris.memory.list_all()
    siris.memory.list_all()

    reloaded = next(m for m in siris.memory.list_all() if m["id"] == entry["id"])
    assert reloaded["recall_count"] == 0


# -- AuditLogStore.failure_counts_by_tool


def test_failure_counts_by_tool_only_includes_tools_with_failures() -> None:
    siris = _fresh_siris()
    tool_a, tool_b = f"tool-a-{uuid.uuid4()}", f"tool-b-{uuid.uuid4()}"
    siris.audit.record(tool_a, {}, result={"ok": True})
    siris.audit.record(tool_b, {}, error="boom")
    siris.audit.record(tool_b, {}, error="boom again")

    counts = siris.audit.failure_counts_by_tool()
    by_name = {c["tool_name"]: c for c in counts}
    assert tool_a not in by_name
    assert by_name[tool_b] == {"tool_name": tool_b, "total": 2, "failures": 2}


def test_failure_counts_sorted_most_failures_first() -> None:
    siris = _fresh_siris()
    flaky, very_flaky = f"flaky-{uuid.uuid4()}", f"very-flaky-{uuid.uuid4()}"
    siris.audit.record(flaky, {}, error="e")
    siris.audit.record(very_flaky, {}, error="e")
    siris.audit.record(very_flaky, {}, error="e")
    siris.audit.record(very_flaky, {}, error="e")

    counts = siris.audit.failure_counts_by_tool()
    names_in_order = [c["tool_name"] for c in counts if c["tool_name"] in (flaky, very_flaky)]
    assert names_in_order.index(very_flaky) < names_in_order.index(flaky)


# -- AuditLogStore.repeated_planner_sequences


def test_repeated_sequence_detected_across_distinct_conversations() -> None:
    siris = _fresh_siris()
    a, b = f"tool-a-{uuid.uuid4()}", f"tool-b-{uuid.uuid4()}"

    for _ in range(3):
        conv = str(uuid.uuid4())
        siris.audit.record(a, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")
        siris.audit.record(b, {}, conversation_id=conv, trigger="planner: step 2 of an in-progress plan")

    sequences = siris.audit.repeated_planner_sequences(min_occurrences=3)
    matching = [s for s in sequences if s["actions"] == [a, b]]
    assert len(matching) == 1
    assert matching[0]["occurrences"] == 3


def test_sequence_below_min_occurrences_is_not_reported() -> None:
    siris = _fresh_siris()
    a, b = f"tool-a-{uuid.uuid4()}", f"tool-b-{uuid.uuid4()}"
    conv = str(uuid.uuid4())
    siris.audit.record(a, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")
    siris.audit.record(b, {}, conversation_id=conv, trigger="planner: step 2 of an in-progress plan")

    sequences = siris.audit.repeated_planner_sequences(min_occurrences=3)
    assert not any(s["actions"] == [a, b] for s in sequences)


def test_single_step_plans_are_not_counted_as_sequences() -> None:
    siris = _fresh_siris()
    a = f"tool-a-{uuid.uuid4()}"
    for _ in range(5):
        conv = str(uuid.uuid4())
        siris.audit.record(a, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")

    sequences = siris.audit.repeated_planner_sequences(min_occurrences=3)
    assert not any(s["actions"] == [a] for s in sequences)


def test_two_separate_plans_in_the_same_conversation_are_not_merged() -> None:
    siris = _fresh_siris()
    a, b, c, d = (f"t-{uuid.uuid4()}" for _ in range(4))
    conv = str(uuid.uuid4())
    # First plan: a -> b. Second plan (step resets to 1): c -> d.
    siris.audit.record(a, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")
    siris.audit.record(b, {}, conversation_id=conv, trigger="planner: step 2 of an in-progress plan")
    siris.audit.record(c, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")
    siris.audit.record(d, {}, conversation_id=conv, trigger="planner: step 2 of an in-progress plan")

    sequences = siris.audit.repeated_planner_sequences(min_occurrences=1)
    actions_seen = [s["actions"] for s in sequences]
    assert [a, b] in actions_seen
    assert [c, d] in actions_seen
    assert [a, b, c, d] not in actions_seen


def test_non_planner_calls_do_not_pollute_sequences() -> None:
    siris = _fresh_siris()
    a, b = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"
    conv = str(uuid.uuid4())
    siris.audit.record(a, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")
    siris.audit.record("unrelated_direct_call", {}, conversation_id=conv, trigger="direct API request")
    siris.audit.record(b, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")

    sequences = siris.audit.repeated_planner_sequences(min_occurrences=1)
    assert not any(s["actions"] == [a, b] for s in sequences)


# -- SelfMaintenanceSkill


def test_self_maintenance_reports_unused_and_duplicate_memories(monkeypatch) -> None:
    siris = _fresh_siris()
    content = f"dup-{uuid.uuid4()}"
    siris.memory.remember(content)
    siris.memory.remember(content)

    import app.skills.builtin.self_maintenance as sm

    monkeypatch.setattr(sm, "_older_than", lambda iso_ts, min_age: True)  # treat everything as old enough
    result = sm.SelfMaintenanceSkill().run(siris)

    assert any(d["content"] == content and len(d["ids"]) == 2 for d in result["duplicate_memories"])
    assert any(m["content"] == content for m in result["unused_memories"])
    assert "unused_memories" in result
    assert "failing_tools" in result
    assert "skill_suggestions" in result


def test_self_maintenance_suggests_a_skill_for_a_repeated_sequence() -> None:
    siris = _fresh_siris()
    a, b = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"
    for _ in range(3):
        conv = str(uuid.uuid4())
        siris.audit.record(a, {}, conversation_id=conv, trigger="planner: step 1 of an in-progress plan")
        siris.audit.record(b, {}, conversation_id=conv, trigger="planner: step 2 of an in-progress plan")

    from app.skills.builtin.self_maintenance import SelfMaintenanceSkill

    result = SelfMaintenanceSkill().run(siris)
    assert any(f"{a} -> {b}" in s for s in result["skill_suggestions"])


def test_self_maintenance_skill_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    assert "self_maintenance_check" in response.json()["tools"]


def test_self_maintenance_runnable_via_direct_endpoint(client: TestClient) -> None:
    response = client.post("/siris/tools/self_maintenance_check/run", json={})
    assert response.status_code == 200
    result = response.json()["tool_call"]["result"]
    for key in ("unused_memories", "duplicate_memories", "failing_tools", "repeated_planner_sequences", "skill_suggestions"):
        assert key in result
