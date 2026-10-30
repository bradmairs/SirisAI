"""The weekly audit digest (app/audit/digest.py)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.audit import digest
from app.core import daily_jobs
from app.core.config import Settings
from app.core.siris import SirisAI
from app.db.models import AttentionItem, ToolExecution
from app.db.session import SessionLocal


def test_the_weekly_digest_counts_what_happened(client: TestClient) -> None:
    with SessionLocal() as session:
        session.query(ToolExecution).delete()
        session.query(AttentionItem).delete()
        session.commit()
    siris = SirisAI(settings=Settings())
    for _ in range(3):
        siris.audit.record("notify", {"title": "x"}, result={"ok": True}, trigger="proactive check: server down")
    siris.audit.record("home_assistant_call_service", {"service": "turn_off"}, result={}, trigger="presence_check: pre-configured away scene")
    siris.audit.record("notify", {"title": "chat"}, result={}, trigger="direct API request: a human explicitly asked")  # not on its own
    siris.audit.record("ssh_power", {"action": "reboot"}, confirmation_required=True, trigger="chat")
    siris.audit.record("ssh_power", {}, error="pin_required: approving ssh power needs your PIN", trigger="attention feed: the user chose 'approve'")
    for n in range(1, 6):
        siris.audit.record("approval_pin", {}, error=f"wrong PIN ({n} in a row)", trigger="approvals: a PIN was checked")
    siris.audit.record("settings_update", {"changed": ["x"]}, result={}, trigger="the Settings page")

    found = digest.weekly(siris, datetime.now(UTC) + timedelta(minutes=1))
    assert found["done_on_own"] == 4 and found["by_tool"] == {"notify": 3, "home_assistant_call_service": 1}
    assert found["held_for_ok"] == 1 and len(found["refused"]) == 1
    assert found["wrong_pins"] == 5 and found["lockouts"] == 1 and found["settings_changes"] == 1
    assert found["text"] == (
        "SirisAI's week: 4 things done on its own (3 notifications, 1 device change), 1 held for your OK, 1 refused. "
        "5 wrong PINs, 1 lockout. Settings changed 1 time."
    )
    item = next(i for i in siris.attention.list() if i["source"] == "audit-weekly")
    assert item["severity"] == "attention" and "Refused ssh power: pin_required" in item["body"]
    assert {j.name: j for j in daily_jobs.JOBS}["audit_weekly"].weekday(siris) == 6
