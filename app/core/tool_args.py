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


# 4.28.0: every call checked against the tool's own argument list before it runs, so a bad call gets a clear
# message the model can correct, instead of an error from deep inside the tool.

_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


def _matches(value: Any, kind: str | list[str] | None) -> bool:
    kinds = kind if isinstance(kind, list) else [kind]
    for k in kinds:
        if k is None or k not in _JSON_TYPES:
            return True
        if k in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, _JSON_TYPES[k]):
            return True
    return value is None


def fit_enums(arguments: dict[str, Any], schema: Any) -> dict[str, Any]:
    """An enum value in the wrong case ("Movie" for "movie") becomes the listed one."""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(arguments, dict) or not isinstance(props, dict):
        return arguments
    out = dict(arguments)
    for k, v in arguments.items():
        allowed = (props.get(k) or {}).get("enum") if isinstance(props.get(k), dict) else None
        if allowed and isinstance(v, str) and v not in allowed:
            match = next((a for a in allowed if isinstance(a, str) and a.lower() == v.strip().lower()), None)
            if match is not None:
                out[k] = match
    return out


def problems(arguments: dict[str, Any], schema: Any) -> list[str]:
    """What's wrong with a call, in words the model can act on. Empty when it's fine.

    Checked: required arguments that are missing or empty, a number or yes/no
    that isn't one (after coerce()), values not in an enum, and numbers outside
    minimum and maximum. Arguments the schema doesn't list, and text-vs-list
    differences (schemas are often loose there), are left for the tool."""
    if not isinstance(schema, dict) or not isinstance(arguments, dict):
        return []
    props = schema.get("properties") or {}
    out: list[str] = []
    for name in schema.get("required") or []:
        if name not in arguments or arguments[name] is None or arguments[name] == "":
            out.append(f"'{name}' is required")
    for name, value in arguments.items():
        spec = props.get(name)
        if not isinstance(spec, dict) or value is None:
            continue
        kinds = spec.get("type") if isinstance(spec.get("type"), list) else [spec.get("type")]
        # Only numbers and yes/no: schemas often say "string" for something the tool also takes as a list.
        if set(kinds) <= {"integer", "number", "boolean"} and not _matches(value, spec.get("type")):
            out.append(f"'{name}' should be {spec.get('type')}, not {type(value).__name__} {value!r}"[:160])
            continue
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            out.append(f"'{name}' must be one of {', '.join(map(str, allowed))}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if spec.get("minimum") is not None and value < spec["minimum"]:
                out.append(f"'{name}' must be at least {spec['minimum']}")
            if spec.get("maximum") is not None and value > spec["maximum"]:
                out.append(f"'{name}' must be at most {spec['maximum']}")
    return out
