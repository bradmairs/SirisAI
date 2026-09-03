import uuid

import httpx
from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.integrations.openapi import discover_get_operations
from app.plugins.builtin.openapi_tool import build_dynamic_tools


def _sample_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "paths": {
            "/status": {
                "get": {
                    "operationId": "get_status",
                    "summary": "Get overall status",
                    "parameters": [],
                }
            },
            "/devices/{device_id}": {
                "get": {
                    "operationId": "get_device",
                    "summary": "Get one device",
                    "parameters": [
                        {"name": "device_id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "verbose", "in": "query", "required": False, "schema": {"type": "boolean"}},
                    ],
                },
                "post": {"operationId": "create_device", "summary": "Should not be discovered"},
            },
            "/complex": {
                "get": {
                    "operationId": "get_complex",
                    "summary": "Uses a $ref-shaped schema",
                    "parameters": [
                        {"name": "filter", "in": "query", "required": False, "schema": {"$ref": "#/components/schemas/Filter"}},
                    ],
                }
            },
        },
    }


# -- discover_get_operations


def test_discovers_only_get_operations() -> None:
    operations = discover_get_operations(_sample_spec())
    names = {op.name for op in operations}
    assert "get_status" in names
    assert "get_device" in names
    assert "create_device" not in names


def test_discovered_operation_captures_path_and_query_params() -> None:
    operations = discover_get_operations(_sample_spec())
    device_op = next(op for op in operations if op.name == "get_device")

    assert device_op.path == "/devices/{device_id}"
    assert device_op.path_params == ["device_id"]
    assert device_op.parameters_schema["required"] == ["device_id"]
    assert device_op.parameters_schema["properties"]["device_id"]["type"] == "string"
    assert device_op.parameters_schema["properties"]["verbose"]["type"] == "boolean"


def test_ref_shaped_schema_falls_back_to_string() -> None:
    operations = discover_get_operations(_sample_spec())
    complex_op = next(op for op in operations if op.name == "get_complex")
    assert complex_op.parameters_schema["properties"]["filter"]["type"] == "string"


def test_operation_without_operation_id_gets_a_derived_name() -> None:
    spec = {"paths": {"/foo/bar": {"get": {"summary": "no id"}}}}
    operations = discover_get_operations(spec)
    assert operations[0].name  # non-empty, sanitized


# -- DynamicOpenAPITool


def test_dynamic_tool_substitutes_path_params_and_forwards_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/devices/abc123"
        assert request.url.params["verbose"] == "true"
        return httpx.Response(200, json={"id": "abc123"})

    operations = discover_get_operations(_sample_spec())
    device_op = next(op for op in operations if op.name == "get_device")
    tools, client = build_dynamic_tools(
        "myapi", [device_op], "http://example.local", transport=httpx.MockTransport(handler)
    )
    tool = tools[0]

    result = tool.run(device_id="abc123", verbose="true")
    assert result == {"id": "abc123"}
    assert tool.name == "myapi_get_device"
    client.close()


def test_dynamic_tool_sends_configured_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(200, json={"ok": True})

    operations = discover_get_operations(_sample_spec())
    status_op = next(op for op in operations if op.name == "get_status")
    tools, client = build_dynamic_tools(
        "myapi",
        [status_op],
        "http://example.local",
        auth_header_name="Authorization",
        auth_header_value="Bearer secret-token",
        transport=httpx.MockTransport(handler),
    )

    assert tools[0].run() == {"ok": True}
    client.close()


def test_one_client_is_shared_across_every_operation_in_an_integration() -> None:
    operations = discover_get_operations(_sample_spec())
    tools, client = build_dynamic_tools("myapi", operations, "http://example.local")
    assert all(tool._client is client for tool in tools)  # noqa: SLF001 -- testing the sharing itself
    client.close()


def test_dynamic_tool_is_always_read_permission() -> None:
    from app.plugins.base import ToolPermission

    operations = discover_get_operations(_sample_spec())
    tools, client = build_dynamic_tools("myapi", [operations[0]], "http://example.local")
    assert tools[0].permission is ToolPermission.READ
    client.close()


# -- IntegrationStore


def test_save_and_list_integration_excludes_secret_by_default() -> None:
    siris = SirisAI(settings=get_siris().settings)
    name = f"myapi-{uuid.uuid4()}"

    saved = siris.integrations.save_integration(
        name, "http://example.local", _sample_spec(), "Authorization", "Bearer secret"
    )
    assert saved["has_auth"] is True
    assert "auth_header_value" not in saved

    listed = siris.integrations.list_integrations()
    matching = next(i for i in listed if i["name"] == name)
    assert "auth_header_value" not in matching


def test_list_integrations_with_include_secrets() -> None:
    siris = SirisAI(settings=get_siris().settings)
    name = f"myapi-{uuid.uuid4()}"
    siris.integrations.save_integration(name, "http://example.local", _sample_spec(), "Authorization", "Bearer secret")

    listed = siris.integrations.list_integrations(include_secrets=True)
    matching = next(i for i in listed if i["name"] == name)
    assert matching["auth_header_value"] == "Bearer secret"


# -- Endpoint-level


def test_register_integration_via_endpoint_makes_tools_immediately_callable(client: TestClient) -> None:
    name = f"myapi-{uuid.uuid4()}"
    response = client.post(
        "/siris/integrations",
        json={"name": name, "base_url": "http://example.local", "spec": _sample_spec()},
    )
    assert response.status_code == 200
    body = response.json()
    assert f"{name}_get_status" in body["discovered_tools"]
    assert f"{name}_create_device" not in " ".join(body["discovered_tools"])

    status_tools = client.get("/siris/status").json()["tools"]
    assert f"{name}_get_status" in status_tools


def test_register_integration_requires_spec_or_spec_url(client: TestClient) -> None:
    response = client.post("/siris/integrations", json={"name": "x", "base_url": "http://example.local"})
    assert response.status_code == 422


def test_register_integration_with_no_get_operations_returns_422(client: TestClient) -> None:
    response = client.post(
        "/siris/integrations",
        json={"name": f"empty-{uuid.uuid4()}", "base_url": "http://example.local", "spec": {"paths": {}}},
    )
    assert response.status_code == 422


def test_list_integrations_endpoint_shows_discovered_tools(client: TestClient) -> None:
    name = f"myapi-{uuid.uuid4()}"
    client.post("/siris/integrations", json={"name": name, "base_url": "http://example.local", "spec": _sample_spec()})

    listed = client.get("/siris/integrations").json()
    matching = next(i for i in listed if i["name"] == name)
    assert f"{name}_get_status" in matching["discovered_tools"]
    assert "auth_header_value" not in matching


def test_discovered_tool_appears_under_discovered_category(client: TestClient) -> None:
    name = f"myapi-{uuid.uuid4()}"
    client.post("/siris/integrations", json={"name": name, "base_url": "http://example.local", "spec": _sample_spec()})

    capabilities = client.get("/siris/capabilities").json()
    assert f"{name}_get_status" in capabilities["discovered"]


def test_registering_the_same_name_twice_skips_duplicates_instead_of_crashing(client: TestClient) -> None:
    name = f"myapi-{uuid.uuid4()}"
    first = client.post("/siris/integrations", json={"name": name, "base_url": "http://example.local", "spec": _sample_spec()})
    second = client.post("/siris/integrations", json={"name": name, "base_url": "http://example.local", "spec": _sample_spec()})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["discovered_tools"] == []  # every name already taken by the first registration


def test_integration_tools_are_re_registered_on_a_fresh_siris_instance() -> None:
    """Simulates a restart: a second SirisAI() built against the same DB
    should pick the integration back up without registering it again."""
    siris_a = SirisAI(settings=get_siris().settings)
    name = f"myapi-{uuid.uuid4()}"
    siris_a.integrations.save_integration(name, "http://example.local", _sample_spec(), None, None)

    siris_b = SirisAI(settings=get_siris().settings)
    assert siris_b.tools.get(f"{name}_get_status") is not None
