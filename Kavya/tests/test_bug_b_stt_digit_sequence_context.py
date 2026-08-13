from tests.test_stt_stream_rotation import _fake_google_speech, _stream
import server


def test_english_speech_context_includes_digit_sequence_oov_class(monkeypatch):
    fake, _recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)

    stt = _stream(lang="en")
    config = stt._streaming_config("en-US", server.STT_ALTERNATIVES.get("en", []), enhanced=True)

    contexts = config.kwargs["config"].kwargs["speech_contexts"]
    assert len(contexts) == 2
    assert contexts[0].kwargs["phrases"] == list(server.EN_STT_PHRASE_LIST[:server.STT_MAX_ADAPTATION_PHRASES])
    assert contexts[0].kwargs["boost"] == server.STT_ADAPTATION_BOOST
    assert contexts[1].kwargs["phrases"] == ["$OOV_CLASS_DIGIT_SEQUENCE"]
    assert contexts[1].kwargs["boost"] == server.STT_DIGIT_CLASS_BOOST


def test_english_speech_context_omits_digit_sequence_oov_class_when_disabled(monkeypatch):
    fake, _recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)
    monkeypatch.setattr(server, "STT_DIGIT_CLASS_BOOST", 0.0)

    stt = _stream(lang="en")
    config = stt._streaming_config("en-US", server.STT_ALTERNATIVES.get("en", []), enhanced=True)

    contexts = config.kwargs["config"].kwargs["speech_contexts"]
    assert len(contexts) == 1
    assert contexts[0].kwargs["phrases"] == list(server.EN_STT_PHRASE_LIST[:server.STT_MAX_ADAPTATION_PHRASES])


def test_english_speech_context_uses_clamped_digit_class_boost():
    assert server._parse_clamped_float(
        {"STT_DIGIT_CLASS_BOOST": "25"},
        "STT_DIGIT_CLASS_BOOST",
        4.0,
        0.0,
        20.0,
    ) == 20.0
    assert server._parse_clamped_float(
        {"STT_DIGIT_CLASS_BOOST": "-7"},
        "STT_DIGIT_CLASS_BOOST",
        4.0,
        0.0,
        20.0,
    ) == 0.0


def test_stt_digit_class_context_obeys_custom_boost(monkeypatch):
    fake, _recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)
    monkeypatch.setattr(server, "STT_DIGIT_CLASS_BOOST", 6.5)

    stt = _stream(lang="en")
    config = stt._streaming_config("en-US", server.STT_ALTERNATIVES.get("en", []), enhanced=True)

    contexts = config.kwargs["config"].kwargs["speech_contexts"]
    assert len(contexts) == 2
    assert contexts[1].kwargs["phrases"] == ["$OOV_CLASS_DIGIT_SEQUENCE"]
    assert contexts[1].kwargs["boost"] == 6.5


def test_privacy_safe_english_configuration_logs_digit_class_state(monkeypatch, caplog):
    fake, _recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)
    monkeypatch.setattr(server, "STT_DIGIT_CLASS_BOOST", 6.5)
    stt = server.GoogleSTTStream(
        on_final_result=lambda *_: None,
        lang="en",
        privacy_safe=True,
    )

    with caplog.at_level("INFO"):
        stt._streaming_config("en-US", [], enhanced=True)

    assert [record.getMessage() for record in caplog.records] == [
        "smartpbx_media event=stt_digit_class_state digit_class_enabled=true digit_class_boost=6.5"
    ]


def test_other_languages_do_not_apply_english_speech_contexts(monkeypatch):
    # `enhanced` is what carries the telephony model + adaptation, and it is
    # gated to English by _use_enhanced_config(); non-en streams can never
    # request it, so exercise the real production gate rather than forcing
    # enhanced=True onto a language the code forbids it for.
    fake, _recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)

    for lang, primary in (("si", "si-LK"), ("ta", "ta-IN")):
        stream = _stream(lang=lang)
        assert stream._use_enhanced_config() is False
        config = stream._streaming_config(
            primary,
            server.STT_ALTERNATIVES.get(lang, []),
            enhanced=stream._use_enhanced_config(),
        )
        assert "speech_contexts" not in config.kwargs["config"].kwargs
        assert "model" not in config.kwargs["config"].kwargs
