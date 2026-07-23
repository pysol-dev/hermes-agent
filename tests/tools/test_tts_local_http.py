import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from gateway.session_context import _UNSET, _VAR_MAP
from tools import tts_tool


def _reset_session_context() -> None:
    for var in _VAR_MAP.values():
        var.set(_UNSET)


@pytest.fixture(autouse=True)
def _clean_session_platform(monkeypatch):
    _reset_session_context()
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    yield
    _reset_session_context()


class _Response:
    def __init__(self, content=b"mp3", status_code=200, content_type="audio/mpeg"):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


def test_local_http_posts_openai_compatible_payload(tmp_path, monkeypatch):
    out = tmp_path / "speech.mp3"
    post = Mock(return_value=_Response(b"audio-bytes"))
    monkeypatch.setattr("requests.post", post)

    cfg = {
        "local_http": {
            "base_url": "http://127.0.0.1:8020/v1",
            "model": "xtts-v2",
            "voice": "peach",
            "timeout": 120,
            "extra_body": {"language": "en", "speaker_wav": "/refs/peach"},
        }
    }

    assert tts_tool._generate_local_http_tts("hello", str(out), cfg) == str(out)
    assert out.read_bytes() == b"audio-bytes"
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args == ("http://127.0.0.1:8020/v1/audio/speech",)
    assert kwargs["json"] == {
        "model": "xtts-v2",
        "input": "hello",
        "voice": "peach",
        "response_format": "mp3",
        "language": "en",
        "speaker_wav": "/refs/peach",
    }
    assert kwargs["headers"]["Accept"] == "audio/*"
    assert kwargs["timeout"] == 120


def test_local_http_uses_endpoint_and_auth(tmp_path, monkeypatch):
    out = tmp_path / "speech.ogg"
    post = Mock(return_value=_Response(b"opus", content_type="audio/ogg"))
    monkeypatch.setattr("requests.post", post)

    cfg = {
        "local_http": {
            "endpoint": "http://localhost:9000/speak",
            "api_key": "secret-token",
        }
    }

    tts_tool._generate_local_http_tts("hello", str(out), cfg)

    args, kwargs = post.call_args
    assert args == ("http://localhost:9000/speak",)
    assert kwargs["json"]["response_format"] == "opus"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


def test_text_to_speech_dispatches_local_http_not_edge(tmp_path, monkeypatch):
    out = tmp_path / "speech.mp3"
    local_http = Mock(side_effect=lambda _text, output_path, _cfg: Path(output_path).write_bytes(b"mp3") or output_path)
    edge = Mock()
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "local_http", "local_http": {}})
    monkeypatch.setattr(tts_tool, "_generate_local_http_tts", local_http)
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", edge)

    result = json.loads(tts_tool.text_to_speech_tool("hello", output_path=str(out)))

    assert result["success"] is True
    assert result["provider"] == "local_http"
    assert result["file_path"] == str(out)
    local_http.assert_called_once()
    edge.assert_not_called()


def test_explicit_unknown_provider_errors_without_edge_fallback(tmp_path, monkeypatch):
    out = tmp_path / "speech.mp3"
    edge = Mock()
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "does-not-exist"})
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", edge)
    monkeypatch.setattr(tts_tool, "_import_edge_tts", Mock())

    result = json.loads(tts_tool.text_to_speech_tool("hello", output_path=str(out)))

    assert result["success"] is False
    assert result["provider"] == "does-not-exist"
    assert "Unsupported TTS provider" in result["error"]
    edge.assert_not_called()
    assert not out.exists()


def test_local_http_failure_errors_without_edge_fallback(tmp_path, monkeypatch):
    out = tmp_path / "speech.mp3"
    local_http = Mock(side_effect=RuntimeError("sidecar unavailable"))
    edge = Mock()
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "local_http", "local_http": {}})
    monkeypatch.setattr(tts_tool, "_generate_local_http_tts", local_http)
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", edge)
    monkeypatch.setattr(tts_tool, "_import_edge_tts", Mock())

    result = json.loads(tts_tool.text_to_speech_tool("hello", output_path=str(out)))

    assert result["success"] is False
    assert "local_http" in result["error"]
    assert "sidecar unavailable" in result["error"]
    local_http.assert_called_once()
    edge.assert_not_called()
    assert not out.exists()


def test_text_to_speech_sanitizes_before_provider_dispatch(tmp_path, monkeypatch):
    out = tmp_path / "speech.mp3"
    captured = {}

    def fake_local_http(text, output_path, _cfg):
        captured["text"] = text
        Path(output_path).write_bytes(b"mp3")
        return output_path

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "local_http", "local_http": {}})
    monkeypatch.setattr(tts_tool, "_generate_local_http_tts", fake_local_http)

    result = json.loads(tts_tool.text_to_speech_tool(
        "Read `/home/lock/.hermes/audio_cache/audio_27edf2b42060.ogg`, "
        "source_mono_24k.wav, dr_hayden_ref_01.wav, audio_27edf2b42060.ogg, "
        "https://example.com, and abcdef1234567890",
        output_path=str(out),
    ))

    assert result["success"] is True
    assert "a file path" in captured["text"]
    assert "an audio file" in captured["text"]
    assert "a link" in captured["text"]
    assert "a hash" in captured["text"]
    for raw in (
        "/home/lock/.hermes/audio_cache/audio_27edf2b42060.ogg",
        "source_mono_24k.wav",
        "dr_hayden_ref_01.wav",
        "audio_27edf2b42060.ogg",
        "https://",
    ):
        assert raw not in captured["text"]


def test_local_http_telegram_default_path_is_voice_compatible(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setattr(tts_tool, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "local_http", "local_http": {}})
    monkeypatch.setattr(
        tts_tool,
        "_generate_local_http_tts",
        lambda _text, output_path, _cfg: Path(output_path).write_bytes(b"ogg") or output_path,
    )

    result = json.loads(tts_tool.text_to_speech_tool("hello"))

    out = Path(result["file_path"])
    assert result["success"] is True
    assert out.parent == tmp_path
    assert out.suffix == ".ogg"
    assert result["voice_compatible"] is True
    assert result["media_tag"] == f"[[audio_as_voice]]\nMEDIA:{out}"
