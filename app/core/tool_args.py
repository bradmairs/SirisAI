"""Tool arguments in the types the tool asks for (4.27.1).

A local model often writes a number as text ("5") or as a float (5.0), or a yes/no as "true". A tool that then
slices a list with it fails ("slice indices must be integers": the nightly self-test's Plex question on 22-25 Nov
2026), and a service given "5.0" can refuse it. Each argument the tool's JSON schema types as an integer, number
or boolean is converted when it plainly is one; anything else is left as it came, for the tool to judge.
"""

from __future__ import annotations

from typing import Any

_TRUE, _FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}


def _one(value: Any, kind: str | list[str] | None) -> Any:
    kinds = kind if isinstance(kind, list) else [kind]
    if value is None or isinstance(value, bool):
        return value
    try:
        if "integer" in kinds:
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, str) and value.strip():
                number = float(value.strip())
                if number.is_integer():
                    return int(number)
        elif "number" in kinds and isinstance(value, str) and value.strip():
            return float(value.strip())
        elif "boolean" in kinds and isinstance(value, str):
            word = value.strip().lower()
            if word in _TRUE:
                return True
            if word in _FALSE:
                return False
    except ValueError:
        pass
    return value


def coerce(arguments: dict[str, Any], schema: Any) -> dict[str, Any]:
    """`arguments` with each one the schema types as integer, number or boolean converted where it plainly is one."""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(arguments, dict) or not isinstance(props, dict):
        return arguments
    return {k: _one(v, (props.get(k) or {}).get("type")) if isinstance(props.get(k), dict) else v for k, v in arguments.items()}
