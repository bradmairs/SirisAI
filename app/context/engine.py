"""Context engine: assembles a small, curated context package before each
LLM call, so a general query like "how's everything looking?" has real
situational awareness -- current system health, recent events, and
memories relevant to the prompt -- without every call sending the whole
database.

Deliberately reads state directly (`Tool.run()`, not
`SirisAI.run_tool()`): this is background context-gathering, not a
deliberate action the model chose to take, so it shouldn't show up in the
audit log (GET /siris/activity) as if it had. It's also best-effort --
nothing here should ever be the reason a chat call fails.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.siris import SirisAI

_MAX_EVENTS = 3
_MAX_MEMORIES = 3


class ContextEngine:
    def build(self, siris: "SirisAI", prompt: str) -> str | None:
        """Return a compact text block to prepend as a system message, or
        None if there's nothing worth including."""
        lines: list[str] = []

        system_tool = siris.tools.get("system_info")
        if system_tool is not None:
            try:
                system = system_tool.run()
                disk_bits = ", ".join(f"{path} {usage['percent_used']}%" for path, usage in system["disk"].items())
                status = f"Current system status: CPU {system['cpu_percent']}%, memory {system['memory']['percent_used']}%"
                if disk_bits:
                    status += f", disk {disk_bits}"
                lines.append(status)
            except Exception:
                pass

        events = siris.events.recent_events(limit=_MAX_EVENTS)
        if events:
            event_bits = "; ".join(
                e["description"] + (f" ({e['entity_name']})" if e["entity_name"] else "") for e in events
            )
            lines.append(f"Recent events: {event_bits}")

        memories = siris.memory.recall(query=prompt, limit=_MAX_MEMORIES)
        if memories:
            lines.append("Relevant memories: " + "; ".join(m["content"] for m in memories))

        return "\n".join(lines) if lines else None
