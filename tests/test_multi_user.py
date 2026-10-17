"""Multi-user recognition (milestone 46): a client-supplied X-Siris-User
header isolates memory and goals per user. See app/core/user_context.py
for the contextvar plumbing and design rationale (a per-request
contextvar rather than threading `user_id` through every Tool.run/
Skill.run signature, since SirisAI's tools are process-wide singletons),
and app/memory/service.py / app/goals/service.py for where each store
actually filters by it.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.core.siris import SirisAI, get_siris
from app.core.user_context import DEFAULT_USER_ID, resolve_user_id
from app.db.migrate import apply_column_patches, apply_index_patches
from app.db.models import Goal
from app.db.session import SessionLocal


def _backdate_goal(goal_id: int, days: int) -> None:
    # Same helper as tests/test_goals.py's -- duplicated locally rather
    # than imported across test modules.
    with SessionLocal() as session:
        goal = session.get(Goal, goal_id)
        goal.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        session.commit()


# -- app/core/user_context.py in isolation


def test_resolve_user_id_defaults_when_header_absent() -> None:
    assert asyncio.run(resolve_user_id(x_siris_user=None)) == DEFAULT_USER_ID


def test_resolve_user_id_defaults_when_header_blank() -> None:
    assert asyncio.run(resolve_user_id(x_siris_user="   ")) == DEFAULT_USER_ID


def test_resolve_user_id_uses_header_value() -> None:
    assert asyncio.run(resolve_user_id(x_siris_user="alice")) == "alice"


# -- MemoryStore isolation (unit-level, explicit user_id)


def test_memory_recall_is_scoped_to_user_id() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex
    siris.memory.remember(f"alice's memory {marker}", user_id="alice-46")
    siris.memory.remember(f"bob's memory {marker}", user_id="bob-46")

    alice_results = siris.memory.recall(query=marker, user_id="alice-46")
    bob_results = siris.memory.recall(query=marker, user_id="bob-46")

    assert [e["content"] for e in alice_results] == [f"alice's memory {marker}"]
    assert [e["content"] for e in bob_results] == [f"bob's memory {marker}"]


def test_memory_list_all_is_scoped_to_user_id() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex
    siris.memory.remember(f"carol's memory {marker}", user_id="carol-46")

    assert any(marker in e["content"] for e in siris.memory.list_all(user_id="carol-46"))
    assert not any(marker in e["content"] for e in siris.memory.list_all(user_id="dave-46"))


# -- GoalStore isolation (unit-level)


def test_goal_list_is_scoped_to_user_id() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = uuid.uuid4().hex
    siris.goals.create_goal(f"Alice's goal {marker}", user_id="alice-46g")
    siris.goals.create_goal(f"Bob's goal {marker}", user_id="bob-46g")

    alice_titles = [g["title"] for g in siris.goals.list_goals(user_id="alice-46g") if marker in g["title"]]
    bob_titles = [g["title"] for g in siris.goals.list_goals(user_id="bob-46g") if marker in g["title"]]

    assert alice_titles == [f"Alice's goal {marker}"]
    assert bob_titles == [f"Bob's goal {marker}"]


def test_update_goal_status_from_a_different_user_is_treated_as_not_found() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Alice's goal {uuid.uuid4()}", user_id="alice-46u")

    assert siris.goals.update_goal_status(goal["id"], "completed", user_id="mallory-46u") is None
    reloaded = siris.goals.list_goals(user_id="alice-46u")
    assert next(g for g in reloaded if g["id"] == goal["id"])["status"] == "active"


def test_add_objective_from_a_different_user_is_treated_as_not_found() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Alice's goal {uuid.uuid4()}", user_id="alice-46o")

    assert siris.goals.add_objective(goal["id"], "sneaky", user_id="mallory-46o") is None
    reloaded = siris.goals.list_goals(user_id="alice-46o")
    assert next(g for g in reloaded if g["id"] == goal["id"])["objectives"] == []


def test_complete_objective_from_a_different_user_is_treated_as_not_found() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Alice's goal {uuid.uuid4()}", objectives=["Step one"], user_id="alice-46c")
    objective_id = goal["objectives"][0]["id"]

    assert siris.goals.complete_objective(objective_id, user_id="mallory-46c") is None
    reloaded = siris.goals.list_goals(user_id="alice-46c")
    matching = next(g for g in reloaded if g["id"] == goal["id"])
    assert matching["objectives"][0]["done"] is False


def test_stale_goals_is_scoped_to_user_id() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Old goal {uuid.uuid4()}", user_id="alice-46s")
    _backdate_goal(goal["id"], days=30)

    assert any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14, user_id="alice-46s"))
    assert not any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14, user_id="bob-46s"))


# -- Endpoint-level, via the real X-Siris-User header (resolve_user_id)


def test_memory_endpoints_are_isolated_by_x_siris_user_header(client: TestClient) -> None:
    marker = uuid.uuid4().hex
    content = f"alice-only memory {marker}"

    create_response = client.post(
        "/siris/memory", json={"content": content}, headers={"X-Siris-User": "alice-http"}
    )
    assert create_response.status_code == 200

    as_alice = client.get("/siris/memory", params={"query": marker}, headers={"X-Siris-User": "alice-http"})
    assert [e["content"] for e in as_alice.json()] == [content]

    as_bob = client.get("/siris/memory", params={"query": marker}, headers={"X-Siris-User": "bob-http"})
    assert as_bob.json() == []

    as_default = client.get("/siris/memory", params={"query": marker})
    assert as_default.json() == []


def test_goals_endpoints_are_isolated_by_x_siris_user_header(client: TestClient) -> None:
    marker = uuid.uuid4().hex
    title = f"alice-only goal {marker}"

    create_response = client.post(
        "/siris/tools/create_goal/run",
        json={"arguments": {"title": title}},
        headers={"X-Siris-User": "alice-http-g"},
    )
    assert create_response.status_code == 200
    goal_id = create_response.json()["tool_call"]["result"]["id"]

    as_alice = client.get("/siris/goals", headers={"X-Siris-User": "alice-http-g"})
    assert any(g["id"] == goal_id for g in as_alice.json())

    as_bob = client.get("/siris/goals", headers={"X-Siris-User": "bob-http-g"})
    assert not any(g["id"] == goal_id for g in as_bob.json())


# -- app/db/migrate.py: patching a pre-milestone-46 SQLite file


def test_apply_column_patches_adds_user_id_to_a_table_that_predates_it() -> None:
    """Simulates a real deployment's DB file: `memories`/`goals` tables
    that already existed before this milestone, so create_all() alone
    (which only creates missing *tables*) would leave them without
    user_id and every query against them would start erroring."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)"))
        conn.execute(text("CREATE TABLE goals (id INTEGER PRIMARY KEY, title TEXT)"))

    apply_column_patches(engine)

    inspector = inspect(engine)
    assert "user_id" in {c["name"] for c in inspector.get_columns("memories")}
    assert "user_id" in {c["name"] for c in inspector.get_columns("goals")}

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO memories (content) VALUES ('hi')"))
        row = conn.execute(text("SELECT user_id FROM memories")).fetchone()
        assert row[0] == "default"


def test_apply_column_patches_is_a_no_op_when_the_column_already_exists() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, user_id TEXT)"))

    apply_column_patches(engine)  # must not raise "duplicate column"
    apply_column_patches(engine)  # calling it twice must also not raise


def test_apply_column_patches_skips_tables_that_do_not_exist_yet() -> None:
    engine = create_engine("sqlite:///:memory:")
    apply_column_patches(engine)  # no tables at all -- a brand-new DB -- must not raise


# -- apply_index_patches() (a performance hardening pass), same pattern as
# apply_column_patches() above: a pre-existing table missing the index
# create_all() would have added on a fresh install.


def test_apply_index_patches_adds_a_missing_index_to_a_table_that_predates_it() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE goals (id INTEGER PRIMARY KEY, created_at DATETIME)"))

    apply_index_patches(engine)

    inspector = inspect(engine)
    index_names = {idx["name"] for idx in inspector.get_indexes("goals")}
    assert "ix_goals_created_at" in index_names


def test_apply_index_patches_is_a_no_op_when_the_index_already_exists() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE goals (id INTEGER PRIMARY KEY, created_at DATETIME)"))

    apply_index_patches(engine)  # must not raise
    apply_index_patches(engine)  # calling it twice must also not raise


def test_apply_index_patches_skips_tables_that_do_not_exist_yet() -> None:
    engine = create_engine("sqlite:///:memory:")
    apply_index_patches(engine)  # no tables at all -- a brand-new DB -- must not raise


# -- WAL mode (a performance hardening pass, see app/db/session.py): lets
# readers and a writer proceed concurrently instead of SQLite's default
# rollback-journal mode serializing them against each other.


def test_sqlite_engine_is_configured_for_wal_mode() -> None:
    with SessionLocal() as session:
        mode = session.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal"


def test_no_header_falls_back_to_the_default_user_consistently(client: TestClient) -> None:
    # One 32-character word: a dashed uuid's 4-character pieces could match
    # inside another test's random hex (found in CI: "9ec9").
    marker = uuid.uuid4().hex
    content = f"default-user memory {marker}"

    client.post("/siris/memory", json={"content": content})

    no_header = client.get("/siris/memory", params={"query": marker})
    assert [e["content"] for e in no_header.json()] == [content]

    explicit_default = client.get("/siris/memory", params={"query": marker}, headers={"X-Siris-User": "default"})
    assert [e["content"] for e in explicit_default.json()] == [content]
