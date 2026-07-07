"""Tests for voice interaction text helpers."""

from tools.voice_interactions import (
    VoiceSurface,
    build_permission_prompt,
    build_turn_start_confirmation,
    concise_heard_text,
    sanitize_for_speech,
)


def test_turn_start_confirmation_acknowledges_without_repeating_request():
    confirmation = build_turn_start_confirmation(
        "Sifrot, add the ACPX stuff to the repo and run the tests",
        assistant_names=("Sifrot", "Cipri"),
    )

    assert confirmation is not None
    assert confirmation.surface == VoiceSurface.VOICE_CHANNEL
    assert confirmation.heard_text == "add the ACPX stuff to the repo and run the tests"
    assert confirmation.spoken_text == "I heard you. I’ll work on that now."
    assert "add the ACPX stuff" not in confirmation.spoken_text


def test_turn_start_confirmation_sanitizes_and_truncates_machine_text():
    heard = concise_heard_text(
        "Cypherot, inspect `/home/lock/.hermes/config.yaml` and https://example.com/abc "
        + "very long " * 50,
        assistant_names=("Cypherot",),
        max_chars=90,
    )

    assert "a file path" in heard
    assert "a link" in heard
    assert "/home/lock" not in heard
    assert "https://" not in heard
    assert heard.endswith("…")
    assert len(heard) <= 91


def test_turn_start_confirmation_returns_none_for_empty_transcript():
    assert build_turn_start_confirmation("   ") is None


def test_permission_prompt_is_spoken_in_voice_and_shown_in_text():
    prompt = build_permission_prompt(
        "restart the Hermes gateway",
        reason="new voice settings only load after restart",
        risk="brief Discord and Telegram interruption",
        targets=("Discord voice adapter", "Telegram gateway"),
        request_id="abc123",
    )

    assert prompt.surface == VoiceSurface.BOTH
    assert prompt.approval_options == ("approve", "deny")
    assert prompt.spoken_text.startswith("I need your permission to restart the Hermes gateway")
    assert "Discord voice adapter" not in prompt.spoken_text
    assert "Telegram gateway" not in prompt.spoken_text
    assert "Risk: brief Discord and Telegram interruption" in prompt.spoken_text
    assert "Please approve or deny it in chat" in prompt.spoken_text
    assert "Permission needed: restart the Hermes gateway." in prompt.text_text
    assert "Request ID: abc123." in prompt.text_text


def test_permission_prompt_sanitizes_targets_for_speech():
    prompt = build_permission_prompt(
        "write files",
        targets=("/home/lock/.hermes/config.yaml", "https://example.com"),
    )

    assert "a file path" not in prompt.spoken_text
    assert "a link" not in prompt.spoken_text
    assert "/home/lock" not in prompt.spoken_text
    assert "https://" not in prompt.spoken_text
    assert "a file path" in prompt.text_text
    assert "a link" in prompt.text_text


def test_sanitize_for_speech_collapses_hayden_paths_filenames_and_code_blocks():
    response = """Generated the Hayden reference set at `~/.hermes/voice_refs/drhayden/multi/`.

Audio cache: /home/lock/.hermes/audio_cache/audio_27edf2b42060.ogg
Files: source_mono_24k.wav, dr_hayden_ref_01.wav, audio_27edf2b42060.ogg

```python
from pathlib import Path
Path('/home/lock/.hermes/audio_cache/audio_27edf2b42060.ogg').read_bytes()
```

Windows fallback: C:\\Users\\Hayden\\voice_refs\\dr_hayden_ref_01.wav
"""

    spoken = sanitize_for_speech(response)

    assert "code block" in spoken
    assert "a file path" in spoken
    assert "an audio file" in spoken
    for raw in (
        "~/.hermes/voice_refs/drhayden/multi/",
        "/home/lock/.hermes/audio_cache/audio_27edf2b42060.ogg",
        "source_mono_24k.wav",
        "dr_hayden_ref_01.wav",
        "audio_27edf2b42060.ogg",
        "C:\\Users\\Hayden\\voice_refs\\dr_hayden_ref_01.wav",
        "read_bytes",
    ):
        assert raw not in spoken
