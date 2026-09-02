"""A trivial reference tool.

Exists to prove the plugin pattern end-to-end (registration, discovery via
`SirisAI.tools`, invocation) before any real homelab/automation tool is
written. Copy this file's shape for the first real tool.
"""

from typing import Any

from app.plugins.base import Tool


class EchoTool(Tool):
    name = "echo"
    description = "Returns whatever message it was given. Reference/test tool."

    def run(self, message: str = "", **kwargs: Any) -> Any:
        return {"echo": message}
