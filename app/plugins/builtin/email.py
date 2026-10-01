"""Email tools (app/email/client.py). Reading and drafting are safe -- a
draft never leaves the mailbox; sending a reply needs confirmation. Message
content is external text, so the injection guard (app/core/taint.py) treats
it as untrusted: an email can't instruct SirisAI to save or send anything."""

from typing import Any

from app.email.client import EmailClient
from app.plugins.base import Tool, ToolPermission


class EmailListUnreadTool(Tool):
    name = "email_list_unread"
    description = (
        "List unread emails from the last few days, newest first, each marked important or not (newsletters, "
        "automated and no-reply senders are not). Doesn't mark anything as read."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "important_only": {"type": "boolean", "description": "Only the important ones."},
            "since_days": {"type": "integer", "description": "How far back to look. Defaults to 3."},
        },
        "required": [],
    }

    def __init__(self, client: EmailClient) -> None:
        self._client = client

    def run(self, important_only: bool = False, since_days: int = 3, **kwargs: Any) -> Any:
        messages = self._client.list_unread(since_days=since_days)
        return [m for m in messages if m["important"]] if important_only else messages


class EmailReadTool(Tool):
    name = "email_read"
    description = "Read one email as plain text (uid from email_list_unread)."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {"uid": {"type": "string"}}, "required": ["uid"]}

    def __init__(self, client: EmailClient) -> None:
        self._client = client

    def run(self, uid: str = "", **kwargs: Any) -> Any:
        return self._client.read(str(uid))


class EmailDraftReplyTool(Tool):
    name = "email_draft_reply"
    description = "Save a reply to an email as a draft in the mailbox (nothing is sent) -- for the user to review and send."
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {"uid": {"type": "string"}, "body": {"type": "string", "description": "The reply text."}},
        "required": ["uid", "body"],
    }

    def __init__(self, client: EmailClient) -> None:
        self._client = client

    def run(self, uid: str = "", body: str = "", **kwargs: Any) -> Any:
        return self._client.save_draft(str(uid), body)


class EmailSendReplyTool(Tool):
    name = "email_send_reply"
    description = "Send a reply to an email right away. Prefer email_draft_reply unless the user clearly wants it sent."
    permission = ToolPermission.CONFIRM_REQUIRED
    parameters = EmailDraftReplyTool.parameters

    def __init__(self, client: EmailClient) -> None:
        self._client = client

    def run(self, uid: str = "", body: str = "", **kwargs: Any) -> Any:
        return self._client.send_reply(str(uid), body)
