import json
import uuid

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.hermes.client import HermesClient
from app.notify.client import NotifyClient
from app.plugins.base import ToolPermission
from app.plugins.builtin.capability_gap import ResolveCapabilityGapTool


def test_report_gap_with_full_detail() -> None:
    siris = SirisAI(settings=get_siris().settings)
    capability = f"check UPS battery health {uuid.uuid4()}"

    gap = siris.capability_gaps.report_gap(
        capability,
        suggested_integration="Network UPS Tools (NUT)",
        suggested_tool_spec={"name": "ups_get_status", "parameters": {}},
    )

    assert gap["requested_capability"] == capability
    assert gap["suggested_integration"] == "Network UPS Tools (NUT)"
    assert gap["suggested_tool_spec"] == {"name": "ups_get_status", "parameters": {}}
    assert gap["created_at"]


def test_report_gap_with_only_the_required_field() -> None:
    siris = SirisAI(settings=get_siris().settings)
    capability = f"bare gap {uuid.uuid4()}"

    gap = siris.capability_gaps.report_gap(capability)

    assert gap["requested_capability"] == capability
    assert gap["suggested_integration"] is None
    assert gap["suggested_tool_spec"] is None


def test_list_gaps_most_recent_first() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    first = siris.capability_gaps.report_gap(f"first {marker}")
    second = siris.capability_gaps.report_gap(f"second {marker}")

    listed = siris.capability_gaps.list_gaps(limit=200)
    ids_in_order = [g["id"] for g in listed if marker in g["requested_capability"]]
    assert ids_in_order == [second["id"], first["id"]]


def test_report_capability_gap_tool_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    assert "report_capability_gap" in response.json()["tools"]


def test_report_capability_gap_via_tool_and_list_endpoint(client: TestClient) -> None:
    capability = f"monitor UPS battery {uuid.uuid4()}"
    response = client.post(
        "/siris/tools/report_capability_gap/run",
        json={
            "arguments": {
                "requested_capability": capability,
                "suggested_integration": "Network UPS Tools (NUT)",
                "suggested_tool_spec": {"name": "ups_get_status", "parameters": {}},
            }
        },
    )
    assert response.status_code == 200
    result = response.json()["tool_call"]["result"]
    assert result["requested_capability"] == capability

    listed = client.get("/siris/capability_gaps").json()
    assert any(g["id"] == result["id"] and g["suggested_integration"] == "Network UPS Tools (NUT)" for g in listed)


def test_report_capability_gap_via_tool_without_optional_fields(client: TestClient) -> None:
    capability = f"bare via endpoint {uuid.uuid4()}"
    response = client.post(
        "/siris/tools/report_capability_gap/run",
        json={"arguments": {"requested_capability": capability}},
    )
    assert response.status_code == 200
    result = response.json()["tool_call"]["result"]
    assert result["suggested_integration"] is None
    assert result["suggested_tool_spec"] is None


def test_capability_gap_appears_in_capabilities_category(client: TestClient) -> None:
    body = client.get("/siris/capabilities").json()
    assert "report_capability_gap" in body["capabilities"]


# -- list_capability_gaps: chat-time parity with GET /siris/capability_gaps


def test_list_capability_gaps_tool_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    assert "list_capability_gaps" in response.json()["tools"]


def test_list_capability_gaps_tool_returns_reported_gap(client: TestClient) -> None:
    capability = f"detect running out of coffee {uuid.uuid4()}"
    client.post(
        "/siris/tools/report_capability_gap/run",
        json={"arguments": {"requested_capability": capability}},
    )

    response = client.post("/siris/tools/list_capability_gaps/run", json={"arguments": {}})

    assert response.status_code == 200
    listed = response.json()["tool_call"]["result"]
    assert any(g["requested_capability"] == capability for g in listed)


def test_list_capability_gaps_tool_respects_limit() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    siris.capability_gaps.report_gap(f"first {marker}")
    siris.capability_gaps.report_gap(f"second {marker}")

    result = siris.run_tool("list_capability_gaps", {"limit": 1})

    assert len(result) == 1


def test_list_capability_gaps_agrees_with_the_rest_endpoint(client: TestClient) -> None:
    capability = f"parity check {uuid.uuid4()}"
    client.post(
        "/siris/tools/report_capability_gap/run",
        json={"arguments": {"requested_capability": capability}},
    )

    via_tool = client.post(
        "/siris/tools/list_capability_gaps/run", json={"arguments": {"limit": 200}}
    ).json()["tool_call"]["result"]
    via_endpoint = client.get("/siris/capability_gaps?limit=200").json()

    tool_ids = {g["id"] for g in via_tool}
    endpoint_ids = {g["id"] for g in via_endpoint}
    assert tool_ids == endpoint_ids


def test_list_capability_gaps_tool_appears_in_capabilities_category(client: TestClient) -> None:
    body = client.get("/siris/capabilities").json()
    assert "list_capability_gaps" in body["capabilities"]


# -- Gap lifecycle: status/resolution_detail, get_gap, dismiss, mark_resolved/mark_failed


def test_a_new_gap_starts_open_with_no_resolution_detail() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"new gap {uuid.uuid4()}")
    assert gap["status"] == "open"
    assert gap["resolution_detail"] is None


def test_get_gap_returns_none_for_unknown_id() -> None:
    siris = SirisAI(settings=get_siris().settings)
    assert siris.capability_gaps.get_gap(999999999) is None


def test_get_gap_round_trips_a_reported_gap() -> None:
    siris = SirisAI(settings=get_siris().settings)
    created = siris.capability_gaps.report_gap(f"get gap {uuid.uuid4()}")
    fetched = siris.capability_gaps.get_gap(created["id"])
    assert fetched == created


def test_dismiss_sets_status_dismissed() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"not worth it {uuid.uuid4()}")

    updated = siris.capability_gaps.dismiss(gap["id"])

    assert updated["status"] == "dismissed"
    assert updated["resolution_detail"] is None


def test_dismiss_returns_none_for_unknown_id() -> None:
    siris = SirisAI(settings=get_siris().settings)
    assert siris.capability_gaps.dismiss(999999999) is None


def test_mark_resolved_sets_status_and_detail() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"resolved gap {uuid.uuid4()}")

    updated = siris.capability_gaps.mark_resolved(gap["id"], "merged in #123")

    assert updated["status"] == "resolved"
    assert updated["resolution_detail"] == "merged in #123"


def test_mark_failed_sets_status_and_detail() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"failed gap {uuid.uuid4()}")

    updated = siris.capability_gaps.mark_failed(gap["id"], "tests didn't pass")

    assert updated["status"] == "failed"
    assert updated["resolution_detail"] == "tests didn't pass"


def test_list_gaps_status_filter() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    open_gap = siris.capability_gaps.report_gap(f"open {marker}")
    dismissed_gap = siris.capability_gaps.report_gap(f"dismissed {marker}")
    siris.capability_gaps.dismiss(dismissed_gap["id"])

    open_only = siris.capability_gaps.list_gaps(limit=200, status="open")
    dismissed_only = siris.capability_gaps.list_gaps(limit=200, status="dismissed")

    open_ids = {g["id"] for g in open_only}
    dismissed_ids = {g["id"] for g in dismissed_only}
    assert open_gap["id"] in open_ids
    assert dismissed_gap["id"] not in open_ids
    assert dismissed_gap["id"] in dismissed_ids
    assert open_gap["id"] not in dismissed_ids


# -- dismiss_capability_gap tool


def test_dismiss_capability_gap_tool_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    assert "dismiss_capability_gap" in response.json()["tools"]


def test_dismiss_capability_gap_tool_round_trip(client: TestClient) -> None:
    capability = f"dismiss via tool {uuid.uuid4()}"
    created = client.post(
        "/siris/tools/report_capability_gap/run",
        json={"arguments": {"requested_capability": capability}},
    ).json()["tool_call"]["result"]

    response = client.post(
        "/siris/tools/dismiss_capability_gap/run",
        json={"arguments": {"gap_id": created["id"]}},
    )

    assert response.status_code == 200
    result = response.json()["tool_call"]["result"]
    assert result["status"] == "dismissed"


def test_dismiss_capability_gap_tool_unknown_id_fails(client: TestClient) -> None:
    response = client.post(
        "/siris/tools/dismiss_capability_gap/run",
        json={"arguments": {"gap_id": 999999999}},
    )
    assert response.status_code == 502


# -- resolve_capability_gap: delegates to Hermes Agent, parses its RESULT marker


def _hermes_client(handler) -> HermesClient:
    return HermesClient(base_url="http://localhost:8420", model="hermes", transport=httpx.MockTransport(handler))


def test_resolve_raises_for_unknown_gap_id() -> None:
    tool = ResolveCapabilityGapTool(
        SirisAI(settings=get_siris().settings).capability_gaps,
        _hermes_client(lambda r: httpx.Response(200, json={"choices": []})),
    )
    try:
        tool.run(gap_id=999999999)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_resolve_merged_marks_gap_resolved_with_full_response_as_detail() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"resolvable {uuid.uuid4()}", suggested_integration="Some API")

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = json.loads(request.content)
        instruction = parsed["messages"][0]["content"]
        assert gap["requested_capability"] in instruction
        assert "Some API" in instruction
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Implemented and merged.\nRESULT: MERGED"}}]},
        )

    tool = ResolveCapabilityGapTool(siris.capability_gaps, _hermes_client(handler))
    result = tool.run(gap_id=gap["id"])

    assert result["status"] == "resolved"
    assert result["attempted"] is True
    assert "RESULT: MERGED" in result["detail"]
    assert siris.capability_gaps.get_gap(gap["id"])["status"] == "resolved"


def test_resolve_not_merged_marks_gap_failed_with_reason() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"not resolvable {uuid.uuid4()}")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "Couldn't get tests passing.\nRESULT: NOT_MERGED: flaky test suite"}}
                ]
            },
        )

    tool = ResolveCapabilityGapTool(siris.capability_gaps, _hermes_client(handler))
    result = tool.run(gap_id=gap["id"])

    assert result["status"] == "failed"
    assert "flaky test suite" in result["detail"]


def test_resolve_with_no_result_marker_is_treated_as_failed_not_assumed_successful() -> None:
    """Hermes forgetting the marker convention must never read as success --
    the whole point of the marker is not fabricating a verdict from prose."""
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"ambiguous reply {uuid.uuid4()}")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "All done, looks great!"}}]})

    tool = ResolveCapabilityGapTool(siris.capability_gaps, _hermes_client(handler))
    result = tool.run(gap_id=gap["id"])

    assert result["status"] == "failed"
    assert "All done, looks great!" in result["detail"]


def test_resolve_does_not_re_attempt_a_non_open_gap() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"already handled {uuid.uuid4()}")
    siris.capability_gaps.dismiss(gap["id"])

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "RESULT: MERGED"}}]})

    tool = ResolveCapabilityGapTool(siris.capability_gaps, _hermes_client(handler))
    result = tool.run(gap_id=gap["id"])

    assert result == {"gap_id": gap["id"], "status": "dismissed", "attempted": False, "detail": None}
    assert calls == []


def test_resolve_capability_gap_is_safe_action_permission() -> None:
    tool = ResolveCapabilityGapTool(
        SirisAI(settings=get_siris().settings).capability_gaps,
        _hermes_client(lambda r: httpx.Response(200, json={"choices": []})),
    )
    assert tool.permission is ToolPermission.SAFE_ACTION


def test_resolve_capability_gap_registered_only_when_hermes_configured() -> None:
    without_hermes = SirisAI(settings=Settings())
    assert without_hermes.tools.get("resolve_capability_gap") is None

    with_hermes = SirisAI(settings=Settings(hermes_base_url="http://localhost:8420"))
    try:
        assert with_hermes.tools.get("resolve_capability_gap") is not None
    finally:
        assert with_hermes._hermes_client is not None
        with_hermes._hermes_client.close()


# -- resolve_capability_gap: notifies on a successful merge (a Jarvis-concept
# follow-up: nothing else would ever tell a human a fix is ready to deploy)


def _mock_notify_client(handler) -> NotifyClient:
    return NotifyClient(base_url="https://ntfy.sh", topic="test-topic", transport=httpx.MockTransport(handler))


def test_resolve_merged_sends_a_plain_notification_without_public_base_url() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"notify me {uuid.uuid4()}")

    notify_calls = []

    def notify_handler(request: httpx.Request) -> httpx.Response:
        notify_calls.append(json.loads(request.content))
        return httpx.Response(200)

    hermes = _hermes_client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "RESULT: MERGED"}}]}))
    notify = _mock_notify_client(notify_handler)
    tool = ResolveCapabilityGapTool(siris.capability_gaps, hermes, notify=notify)

    tool.run(gap_id=gap["id"])

    assert len(notify_calls) == 1
    assert notify_calls[0]["title"] == "SirisAI: fix ready to deploy"
    assert gap["requested_capability"] in notify_calls[0]["message"]
    assert "actions" not in notify_calls[0]
    hermes.close()
    notify.close()


def test_resolve_merged_includes_a_rebuild_action_button_with_public_base_url() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"notify with button {uuid.uuid4()}")

    notify_calls = []

    def notify_handler(request: httpx.Request) -> httpx.Response:
        notify_calls.append(json.loads(request.content))
        return httpx.Response(200)

    hermes = _hermes_client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "RESULT: MERGED"}}]}))
    notify = _mock_notify_client(notify_handler)
    tool = ResolveCapabilityGapTool(
        siris.capability_gaps,
        hermes,
        notify=notify,
        public_base_url="https://siris.example.com",
        api_key="secret",
    )

    tool.run(gap_id=gap["id"])

    action = notify_calls[0]["actions"][0]
    assert action["action"] == "http"
    assert action["label"] == "Rebuild now"
    assert action["url"] == "https://siris.example.com/siris/tools/rebuild_and_restart/run"
    assert action["method"] == "POST"
    assert action["headers"]["Authorization"] == "Bearer secret"
    assert json.loads(action["body"]) == {"arguments": {}, "confirmed": True}
    hermes.close()
    notify.close()


def test_resolve_not_merged_does_not_send_a_notification() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"no notify on failure {uuid.uuid4()}")

    notify_calls = []

    def notify_handler(request: httpx.Request) -> httpx.Response:
        notify_calls.append(json.loads(request.content))
        return httpx.Response(200)

    hermes = _hermes_client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "RESULT: NOT_MERGED: nope"}}]})
    )
    notify = _mock_notify_client(notify_handler)
    tool = ResolveCapabilityGapTool(siris.capability_gaps, hermes, notify=notify)

    tool.run(gap_id=gap["id"])

    assert notify_calls == []
    hermes.close()
    notify.close()


def test_resolve_merged_without_notify_configured_does_not_raise() -> None:
    siris = SirisAI(settings=get_siris().settings)
    gap = siris.capability_gaps.report_gap(f"no notify configured {uuid.uuid4()}")

    hermes = _hermes_client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "RESULT: MERGED"}}]}))
    tool = ResolveCapabilityGapTool(siris.capability_gaps, hermes)

    result = tool.run(gap_id=gap["id"])

    assert result["status"] == "resolved"
    hermes.close()
