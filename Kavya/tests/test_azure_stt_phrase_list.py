"""Azure STT should bias recognition toward the booking/number domain.

The English Azure recognizer (SmartPBX / direct-English path) is configured
bare, so digit strings, room names and booking terms are mis-heard. A
PhraseListGrammar biases it. Direct SmartPBX Sinhala gets its own,
number-word-only phrase list (`SI_STT_PHRASE_LIST`) so the tens/units/teens
Azure is biased toward match `_normalize_sinhala_spoken_digits`. Tamil/Arabic
are left untouched — phrase lists are language-specific and only English and
Sinhala have one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server


class _PhraseGrammar:
    def __init__(self):
        self.phrases: list[str] = []

    def addPhrase(self, phrase):
        self.phrases.append(phrase)


class _PhraseListGrammarFactory:
    def __init__(self):
        self.grammars: list[_PhraseGrammar] = []

    def from_recognizer(self, _recognizer):
        grammar = _PhraseGrammar()
        self.grammars.append(grammar)
        return grammar


def _signal():
    return SimpleNamespace(connect=lambda _cb: None)


def _fake_azure(factory):
    class SpeechConfig:
        def __init__(self, **_kwargs):
            self.speech_recognition_language = None

    class Recognizer:
        def __init__(self, **_kwargs):
            self.recognizing = _signal()
            self.recognized = _signal()
            self.canceled = _signal()

        def start_continuous_recognition_async(self):
            return None

    audio = SimpleNamespace(
        AudioStreamFormat=lambda **_k: object(),
        PushAudioInputStream=lambda **_k: object(),
        AudioConfig=lambda **_k: object(),
    )
    return SimpleNamespace(
        SpeechConfig=SpeechConfig,
        SpeechRecognizer=Recognizer,
        audio=audio,
        PhraseListGrammar=factory,
    )


def _run_start(monkeypatch, lang):
    factory = _PhraseListGrammarFactory()
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", True)
    monkeypatch.setattr(server, "AZURE_SPEECH_KEY", "test-key")
    monkeypatch.setattr(server, "AZURE_SPEECH_REGION", "southeastasia")
    monkeypatch.setattr(server, "azure_speech", _fake_azure(factory))
    if server.audioop is None:  # pragma: no cover - audioop present on <3.13
        monkeypatch.setattr(server, "audioop", SimpleNamespace())

    stream = server.AzureSTTStream(on_final_result=lambda *_: None, lang=lang)
    stream.start()
    return factory


def test_phrase_list_constant_is_maintainable_and_domain_specific():
    phrases = server.EN_STT_PHRASE_LIST
    assert "Hatton Hills" in phrases
    for room in (
        "Forest Escape Suite", "Eco Harmony Suite", "Sunrise Vista Premium Suite",
        "Mount Luxe Chalet", "Mount Monarch Chalet",
    ):
        assert room in phrases, f"room name {room} must be in the phrase list"
    for term in ("check-in", "check-out", "honeymoon", "half board", "adults", "nights"):
        assert term in phrases, f"booking term {term} must be in the phrase list"
    for digit in ("zero", "one", "seven", "nine", "treble", "nought", "naught"):
        assert digit in phrases, f"digit word {digit} must be in the phrase list"


def test_room_names_are_derived_from_the_shared_vocabulary_not_relisted():
    # Maintainability: the room names come from the single tools source of truth.
    import tools

    for room in tools.ROOM_TYPES_BY_PROPERTY[tools.PROPERTY_HATTON]:
        assert room in server.EN_STT_PHRASE_LIST


def test_english_digit_repeat_terms_are_present_in_stt_phrase_list():
    for term in ("double", "triple", "treble", "nought"):
        assert term in server.EN_STT_PHRASE_LIST, f"digit term {term} is required in EN_STT_PHRASE_LIST"


def test_english_recognizer_gets_the_phrase_list_populated(monkeypatch):
    factory = _run_start(monkeypatch, lang="en")

    assert len(factory.grammars) == 1, "English must get exactly one phrase grammar"
    added = factory.grammars[0].phrases
    assert set(server.EN_STT_PHRASE_LIST) <= set(added)
    assert "Mount Monarch Chalet" in added
    assert "seven" in added


@pytest.mark.parametrize("lang", ["ta", "ar"])
def test_non_english_non_sinhala_paths_get_no_phrase_list(monkeypatch, lang):
    factory = _run_start(monkeypatch, lang=lang)
    assert factory.grammars == [], (
        f"{lang} must not receive any phrase list — phrase lists are "
        "language-specific and the owner keeps Azure for these languages as-is"
    )


def test_sinhala_phrase_list_constant_covers_units_tens_and_teens():
    phrases = server.SI_STT_PHRASE_LIST
    for word in ("එක", "පහ", "නවය", "හැට", "විසි", "පනස්", "එකොළහ", "දහනවය", "දහය"):
        assert word in phrases, f"Sinhala number word {word} must be in the phrase list"


def test_sinhala_recognizer_gets_the_sinhala_phrase_list_populated(monkeypatch):
    factory = _run_start(monkeypatch, lang="si")

    assert len(factory.grammars) == 1, "Sinhala must get exactly one phrase grammar"
    added = factory.grammars[0].phrases
    assert set(server.SI_STT_PHRASE_LIST) <= set(added)
    # It must NOT receive the full English digit-word/booking-term list —
    # different vocabulary (EN_STT_PHRASE_LIST digit words are English-only).
    assert "seven" not in added
    # 2026-09-04 tester feedback: Sinhala DOES now get the five room names
    # (and their component/transliterated words) so Azure si-LK is biased
    # toward the terms callers actually code-switch into — this is the
    # narrow, deliberate exception to "no English word list" above.
    assert "Mount Monarch Chalet" in added
    assert "Suite" in added
