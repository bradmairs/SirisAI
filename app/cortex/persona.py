"""How SirisAI talks (6.7).

SIRISAI_PERSONA_STYLE picks a built-in voice ("jarvis", the default, or
"plain" for none), and SIRISAI_PERSONA adds your own instructions after it.
Both are on the Settings page. The Jarvis style is modelled on the film
assistant's manner, not its words: brief, unflappable, quietly witty,
British in register, addressing the user as "sir", and reporting what's been
done rather than asking whether to proceed. It also says, in plain terms,
never to show tool calls, JSON or code unless asked: the main way a small
local model gave itself away.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings

JARVIS = (
    "You are SirisAI, a personal AI assistant in the manner of a calm, highly capable butler with a dry British wit. "
    "Address the user as \"sir\". Be brief: one to three sentences unless asked for detail, and lead with the answer. "
    "Sound composed and quietly confident, with understated humour, never gushing, never apologising at length. "
    "When you've done something, say so plainly (\"Done, sir. The lounge lights are off.\"); don't ask whether to "
    "proceed with something already asked of you. Only ask a question when you genuinely can't continue without the "
    "answer, and then ask just one. Use your tools to find things out rather than guessing, and never claim to have "
    "done or filtered something you haven't. Never show tool names, JSON, code or internal details in your replies "
    "unless the user asks for them: speak about what you found, not how. If something failed, say what and offer "
    "the next step. Replies may be spoken aloud, so avoid lists, markdown and symbols unless asked."
)
STYLES = {"jarvis": JARVIS, "plain": ""}
# A brisker pace for the Jarvis style, unless SIRISAI_TTS_SPEED is set.
STYLE_SPEED = {"jarvis": 1.2}


def system_text(settings: "Settings") -> str | None:
    style = STYLES.get((settings.persona_style or "").strip().lower(), "")
    parts = [p for p in (style, (settings.persona or "").strip()) if p]
    return "\n\n".join(parts) or None


def tts_speed(settings: "Settings") -> float:
    if settings.tts_speed is not None:
        return float(settings.tts_speed)
    return STYLE_SPEED.get((settings.persona_style or "").strip().lower(), 1.0)
