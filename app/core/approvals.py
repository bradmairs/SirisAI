"""Safer approvals for the big actions: a PIN, and approvals that expire.

- **A PIN** (SIRISAI_APPROVAL_PIN). With one set, approving a high-risk action
  needs it, wherever the approval comes from: chat's Allow, the attention
  feed (SirisOS, the web UI), `POST /siris/tools/{name}/run`, the feed's
  Reboot / Install updates / Restart buttons, and saving settings. High-risk
  means rebooting or shutting down servers, installing updates, and SSH
  commands that change things (plus SIRISAI_APPROVAL_PIN_TOOLS). So someone
  holding your unlocked phone or the wall tablet can't do them with a tap.
  Five wrong PINs lock these approvals for 10 minutes and raise an urgent
  alert. Spoken "yes" never approves them, and phone notification buttons
  only offer Decline for them (a button can't carry a PIN).
- **Expiry** (SIRISAI_APPROVAL_EXPIRY_MINUTES, 15): an action held for
  approval in the attention feed ("Approve: reboot all") can't be approved
  after that long; the item closes as expired. Synced items such as "nas
  needs a reboot" are re-checked on their own schedule, so they don't expire.

The HTTP layer checks the PIN and marks the request verified
(`pin_verified()`); `SirisAI.run_tool()` refuses a high-risk confirmed call
that wasn't (`require()`), so nothing gets past by a side door.
"""

import hmac
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

HIGH_RISK_TOOLS = frozenset({"ssh_power", "ssh_install_updates", "ssh_run"})
# Feed buttons that do a high-risk thing directly (app/servers/watch.py).
HIGH_RISK_ACTIONS = frozenset({("server-updates", "reboot"), ("server-updates", "install"), ("servers", "restart")})
LOCK_AFTER = 5
LOCK_FOR = timedelta(minutes=10)
FAILURES_KEY = "approval_pin_failures"

_pin_ok: ContextVar[bool] = ContextVar("approval_pin_ok", default=False)


class PinRequired(PermissionError):
    """403 with detail starting 'pin_required' -- clients prompt and retry."""

    def __init__(self, what: str = "this") -> None:
        super().__init__(f"pin_required: approving {what} needs your PIN")


class PinRejected(PermissionError):
    pass


class ApprovalExpired(LookupError):
    pass


def pin_enabled(siris: "SirisAI") -> bool:
    return bool(getattr(siris.settings, "approval_pin", ""))


def high_risk_tools(siris: "SirisAI") -> set[str]:
    extra = {t.strip() for t in siris.settings.approval_pin_tools.split(",") if t.strip()}
    return set(HIGH_RISK_TOOLS) | extra


def tool_needs_pin(siris: "SirisAI", tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
    """Whether running this call, confirmed, needs the PIN. A call that
    wouldn't ask for confirmation anyway (a look-only ssh_run) doesn't."""
    if not pin_enabled(siris) or tool_name not in high_risk_tools(siris):
        return False
    tool = siris.tools.get(tool_name)
    return tool is None or tool.needs_confirmation(arguments or {})


def action_needs_pin(siris: "SirisAI", item: dict[str, Any], action_id: str, payload: dict[str, Any]) -> bool:
    if not pin_enabled(siris):
        return False
    if (item["source"], action_id) in HIGH_RISK_ACTIONS:
        return True
    return action_id == "approve" and item["kind"] == "confirm" and tool_needs_pin(siris, str(payload.get("tool")), payload.get("arguments"))


def check_pin(siris: "SirisAI", pin: str | None, now: datetime | None = None) -> None:
    """Raises PinRequired (none given), PinRejected (wrong or locked out)."""
    if not pin_enabled(siris):
        return
    if not pin:
        raise PinRequired()
    now = now or datetime.now(UTC)
    failures = dict(siris.state.get(FAILURES_KEY) or {})
    until = failures.get("locked_until")
    if until and datetime.fromisoformat(until) > now:
        raise PinRejected(f"Too many wrong PINs: big approvals are locked until {until[11:16]} UTC.")
    if hmac.compare_digest(str(pin).strip().encode(), siris.settings.approval_pin.encode()):
        if failures:
            siris.state.delete(FAILURES_KEY)
        return
    count = int(failures.get("count", 0)) + 1
    failures = {"count": count}
    if count >= LOCK_AFTER:
        failures["locked_until"] = (now + LOCK_FOR).isoformat()
        _alert_lockout(siris)
    siris.state.set(FAILURES_KEY, failures)
    siris.audit.record("approval_pin", {}, error=f"wrong PIN ({count} in a row)", trigger="approvals: a PIN was checked")
    left = LOCK_AFTER - count
    raise PinRejected("Wrong PIN." + (f" {left} more and big approvals lock for 10 minutes." if left > 0 else " Big approvals are locked for 10 minutes."))


def _alert_lockout(siris: "SirisAI") -> None:
    try:
        from app.upkeep.alerts import raise_alert

        raise_alert(
            siris,
            "approvals:locked",
            "Wrong PIN entered 5 times",
            "Someone entered the wrong approval PIN five times in a row. Big approvals are locked for 10 minutes.",
            severity="urgent",
            push=True,
        )
    except Exception:  # noqa: BLE001 - the lockout itself still holds
        logger.exception("Alerting on a PIN lockout failed")


@contextmanager
def pin_verified() -> Iterator[None]:
    token = _pin_ok.set(True)
    try:
        yield
    finally:
        _pin_ok.reset(token)


def mark_verified() -> None:
    """For a request whose PIN was just checked: the rest of this request's
    context (including tasks and threads it starts) counts as verified."""
    _pin_ok.set(True)


def require(siris: "SirisAI", tool_name: str, arguments: dict[str, Any]) -> None:
    """Called by run_tool() for a confirmed call that needed confirmation."""
    if not _pin_ok.get() and tool_needs_pin(siris, tool_name, arguments):
        raise PinRequired(tool_name.replace("_", " "))


def expired(siris: "SirisAI", item: dict[str, Any], now: datetime | None = None) -> bool:
    minutes = siris.settings.approval_expiry_minutes
    if item["kind"] != "confirm" or not minutes or minutes <= 0:
        return False
    raised = datetime.fromisoformat(item["updated_at"])
    raised = raised if raised.tzinfo else raised.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - raised > timedelta(minutes=minutes)


def close_expired(siris: "SirisAI", now: datetime | None = None) -> int:
    """Close every held action past its expiry (run with the attention sync)."""
    closed = 0
    for item in siris.attention.list("open", limit=500):
        if expired(siris, item, now):
            siris.attention.close(item["id"], "dismissed", f"Expired: approvals last {siris.settings.approval_expiry_minutes:g} minutes")
            closed += 1
    return closed
