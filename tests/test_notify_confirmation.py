"""Direct tests for the shared confirmation-needed notification helper
(app/notify/confirmation.py) -- RoutineRunner's own behavioral coverage
(tests/test_routines.py) exercises the same code through run_due(); these
check it works called directly, since Cortex (tests/test_cortex.py) is now
a second caller."""

import json
from typing import Any

from app.notify.confirmation import notify_confirmation_needed
from app.plugins.base import ConfirmationRequired


class _FakeTools:
    def __init__(self, notify_present: bool) -> None:
        self._notify_present = notify_present

    def get(self, name: str) -> object | None:
        return object() if name == "notify" and self._notify_present else None


class _FakeSettings:
    def __init__(self, public_base_url: str = "", api_key: str | None = None) -> None:
        self.public_base_url = public_base_url
        self.api_key = api_key


class _FakeSiris:
    def __init__(self, notify_present: bool = True, public_base_url: str = "", api_key: str | None = None) -> None:
        self.tools = _FakeTools(notify_present)
        self.settings = _FakeSettings(public_base_url, api_key)
        self.run_tool_calls: list[tuple[str, dict[str, Any], str]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], trigger: str | None = None, **kwargs: Any) -> Any:
        self.run_tool_calls.append((name, arguments, trigger or ""))
        return {"sent": True}


def test_does_nothing_without_a_notify_channel() -> None:
    siris = _FakeSiris(notify_present=False)
    notify_confirmation_needed(siris, "SirisAI", "dangerous_action", {"target": "prod"})
    assert siris.run_tool_calls == []


def test_sends_a_confirm_button_with_public_base_url_configured() -> None:
    siris = _FakeSiris(notify_present=True, public_base_url="https://siris.example.com", api_key="secret-key")
    notify_confirmation_needed(siris, "SirisAI", "dangerous_action", {"target": "prod"})

    name, args, trigger = siris.run_tool_calls[0]
    assert name == "notify"
    assert args["title"] == "SirisAI: confirmation needed"
    assert "dangerous_action" in args["message"] and "SirisAI wants to run" in args["message"]
    action = args["actions"][0]
    assert action == {
        "action": "http",
        "label": "Confirm",
        "url": "https://siris.example.com/siris/tools/dangerous_action/run",
        "method": "POST",
        "headers": {"Content-Type": "application/json", "Authorization": "Bearer secret-key"},
        "body": json.dumps({"arguments": {"target": "prod"}, "confirmed": True}),
    }
    assert trigger == "SirisAI: action needed confirmation"


def test_no_action_button_without_public_base_url() -> None:
    siris = _FakeSiris(notify_present=True, public_base_url="")
    notify_confirmation_needed(siris, "SirisAI", "dangerous_action", {})
    assert siris.run_tool_calls[0][1]["actions"] is None


def test_the_context_label_appears_in_the_message() -> None:
    siris = _FakeSiris(notify_present=True)
    notify_confirmation_needed(siris, "Routine 'nightly check'", "dangerous_action", {})
    assert siris.run_tool_calls[0][1]["message"].startswith("Routine 'nightly check' wants to run")


def test_evidence_carries_the_pending_tool_and_arguments() -> None:
    siris = _FakeSiris(notify_present=True)
    exc = ConfirmationRequired("dangerous_action", {"target": "prod"})
    notify_confirmation_needed(siris, "SirisAI", exc.tool_name, exc.arguments)
    assert siris.run_tool_calls[0][1]["message"].count("prod") == 1
