"""Minimal OpenAPI 3.x parsing for automatic tool discovery (milestone
32). Deliberately hand-rolled rather than a full OpenAPI library: this
only needs to turn simple GET operations with inline parameter schemas
into Tool-shaped specs, not validate or fully resolve an arbitrary spec.

Known, accepted limitation: doesn't resolve `$ref` or schema composition
(allOf/oneOf/anyOf) -- a parameter using those falls back to a plain
string type rather than failing the whole import. A slightly-imprecise
tool is more useful than none, and a real OpenAPI library is the honest
answer if a spec needs more than this covers -- not something worth
hand-rolling further into this file.
"""

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DiscoveredOperation:
    name: str
    description: str
    path: str
    path_params: list[str]
    parameters_schema: dict[str, Any] = field(default_factory=dict)


def _sanitize_name(raw: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw).strip("_").lower()
    return name or "operation"


def discover_get_operations(spec: dict[str, Any]) -> list[DiscoveredOperation]:
    """Every GET operation in an OpenAPI 3.x spec's `paths`, turned into a
    Tool-shaped spec. GET only -- see app/plugins/builtin/openapi_tool.py's
    module docstring for why mutating operations aren't auto-discovered
    at all in this milestone."""
    operations = []
    for path, path_item in (spec.get("paths") or {}).items():
        operation = (path_item or {}).get("get")
        if operation is None:
            continue

        operation_id = operation.get("operationId") or f"get_{path}"
        name = _sanitize_name(operation_id)
        description = operation.get("summary") or operation.get("description") or f"GET {path}"

        properties: dict[str, Any] = {}
        required: list[str] = []
        path_params: list[str] = []
        for param in operation.get("parameters") or []:
            param_name = param.get("name")
            if not param_name:
                continue
            schema = param.get("schema") or {}
            param_type = schema.get("type")
            if param_type not in ("string", "integer", "number", "boolean"):
                param_type = "string"  # honest fallback for $ref/complex schemas
            properties[param_name] = {"type": param_type, "description": param.get("description", "")}
            if param.get("required"):
                required.append(param_name)
            if param.get("in") == "path":
                path_params.append(param_name)

        operations.append(
            DiscoveredOperation(
                name=name,
                description=description,
                path=path,
                path_params=path_params,
                parameters_schema={"type": "object", "properties": properties, "required": required},
            )
        )
    return operations
