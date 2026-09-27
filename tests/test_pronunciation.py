"""How SirisAI's own name is rewritten for speech synthesis (a direct user
request: "Siris" read wrong on TTS, wanted read as "Cyrus" -- the written
name stays "Siris"/"SirisAI" everywhere else). See tests/test_voice.py and
tests/test_satellites.py for this actually being applied at both call
sites that reach a TTS backend."""

from app.voice.pronunciation import for_speech


def test_sirisai_becomes_cyrus_ai() -> None:
    assert for_speech("SirisAI can help with that.") == "Cyrus AI can help with that."


def test_bare_siris_becomes_cyrus() -> None:
    assert for_speech("Hi, I'm Siris.") == "Hi, I'm Cyrus."


def test_sirisai_is_matched_before_the_shorter_siris_pattern() -> None:
    """The wrong pass order would turn "SirisAI" into "CyrusAI" (bare
    Siris replaced first, leaving "AI" stuck on with no space)."""
    assert for_speech("Ask SirisAI anything.") == "Ask Cyrus AI anything."


def test_matching_is_case_insensitive() -> None:
    assert for_speech("siris and SIRISAI both count") == "Cyrus and Cyrus AI both count"


def test_a_word_that_merely_contains_siris_is_left_alone() -> None:
    assert for_speech("Sirisology is not a word") == "Sirisology is not a word"


def test_text_with_no_occurrences_is_returned_unchanged() -> None:
    assert for_speech("The weather is fine today.") == "The weather is fine today."


def test_multiple_occurrences_are_all_replaced() -> None:
    assert for_speech("SirisAI, this is Siris speaking for SirisAI.") == "Cyrus AI, this is Cyrus speaking for Cyrus AI."
