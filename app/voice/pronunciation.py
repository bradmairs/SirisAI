"""How SirisAI's own name should sound out loud, distinct from how it's
written. A direct user request: "Siris" read literally comes out wrong on
TTS, but the written name (chat replies, the UI, memory, docs) stays
"Siris"/"SirisAI" -- this only rewrites the text handed to speech
synthesis, right before that one call, never anything actually displayed,
stored, or spoken back into a transcript.

Order matters: "SirisAI" is matched (and replaced) before the bare
"Siris" pattern, so "SirisAI" doesn't first get shortened to "Cyrus" + a
dangling "AI" is deliberately produced instead, which is correct and
readable and would be lost if the two passes ran in the other order.
"""

import re

_SIRISAI_RE = re.compile(r"\bSirisAI\b", re.IGNORECASE)
_SIRIS_RE = re.compile(r"\bSiris\b", re.IGNORECASE)


def for_speech(text: str) -> str:
    text = _SIRISAI_RE.sub("Cyrus AI", text)
    text = _SIRIS_RE.sub("Cyrus", text)
    return text
