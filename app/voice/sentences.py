"""Turn a stream of answer-text deltas into speakable sentences, so a voice
reply can start playing as soon as the first sentence exists instead of
after the whole answer is written (POST /siris/voice/converse).

Markdown is for the screen, not the speaker: headings, emphasis markers,
bullets, inline code and links are reduced to the words they carry, and
fenced code blocks are skipped entirely -- read aloud they're noise.
"""

import re

# A sentence ends at . ! ? (or an ellipsis), optionally followed by a closing
# quote/bracket, then whitespace. A blank line or a newline also ends one:
# list items and paragraphs arrive newline-separated with no full stop.
_BOUNDARY_RE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+|\n+")
_ABBREVIATIONS = {"e.g.", "i.e.", "etc.", "vs.", "dr.", "mr.", "mrs.", "ms.", "st.", "no.", "approx.", "min.", "max."}

_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL_RE = re.compile(r"https?://\S+")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_LINE_PREFIX_RE = re.compile(r"^\s*(#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{2,}")


def speakable(text: str) -> str:
    """One sentence/line of markdown reduced to plain words for TTS."""
    if _TABLE_RULE_RE.match(text):
        return ""
    text = _LINE_PREFIX_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("a link", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = text.replace("|", ", ").replace("#", "")
    return re.sub(r"\s+", " ", text).strip(" ,")


def strip_spoken_prefix(spoken: str, final: str) -> str:
    """The part of `final` not already covered by `spoken`, comparing
    without regard to whitespace (a planner's final text may be trimmed
    differently from the streamed deltas). If the two diverge, the rest of
    `final` from the start of the diverging word is returned."""
    i = j = 0
    target = re.sub(r"\s+", "", spoken)
    while j < len(target) and i < len(final):
        if final[i].isspace():
            i += 1
            continue
        if final[i] != target[j]:
            while i > 0 and not final[i - 1].isspace():
                i -= 1
            break
        i += 1
        j += 1
    return final[i:]


class SentenceChunker:
    """Feed answer text in arbitrary pieces; get back complete sentences
    (raw text, not yet `speakable()`) as soon as each one ends. `flush()`
    returns whatever is left once the answer is complete."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_code = False
        # Raw text already turned into sentences -- always a prefix of
        # everything fed, which is what lets a caller work out which part of
        # a final answer hasn't been spoken yet (strip_spoken_prefix).
        self.consumed = ""

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        sentences: list[str] = []
        search_from = 0
        while True:
            match = _BOUNDARY_RE.search(self._buffer, search_from)
            if match is None:
                break
            candidate = self._buffer[: match.start()]
            last_word = candidate.rsplit(None, 1)[-1].lower() if candidate.strip() else ""
            if "\n" not in match.group() and last_word in _ABBREVIATIONS:
                search_from = match.end()
                continue
            self.consumed += self._buffer[: match.end()]
            self._buffer = self._buffer[match.end() :]
            search_from = 0
            sentences.extend(self._accept(candidate))
        return sentences

    def flush(self) -> list[str]:
        rest, self._buffer = self._buffer, ""
        self.consumed += rest
        return self._accept(rest)

    def _accept(self, raw: str) -> list[str]:
        out: list[str] = []
        for line in raw.split("\n"):
            if line.strip().startswith("```"):
                self._in_code = not self._in_code
                continue
            if self._in_code or not line.strip():
                continue
            out.append(line.strip())
        return out
