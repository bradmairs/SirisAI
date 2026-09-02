import uuid

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris


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
