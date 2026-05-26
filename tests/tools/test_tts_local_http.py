"""Tests for the generic local HTTP TTS provider."""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("HERMES_SESSION_PLATFORM",):
        monkeypatch.delenv(key, raising=False)


class TestGenerateLocalHttpTts:
    def test_posts_openai_compatible_payload_and_writes_audio(self, tmp_path):
        from tools.tts_tool import _generate_local_http_tts

        response = MagicMock()
        response.status_code = 200
        response.content = b"fake-audio"
        response.headers = {"content-type": "audio/mpeg"}
        response.raise_for_status = MagicMock()

        config = {
            "local_http": {
                "base_url": "http://127.0.0.1:8123/v1",
                "model": "xtts-v2",
                "voice": "peach",
                "api_key": "local-secret",
                "timeout": 12,
                "extra_body": {"language": "en"},
            }
        }

        output_path = str(tmp_path / "out.mp3")
        with patch("requests.post", return_value=response) as mock_post:
            result = _generate_local_http_tts("Hello world", output_path, config)

        assert result == output_path
        assert (tmp_path / "out.mp3").read_bytes() == b"fake-audio"
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0]
        kwargs = mock_post.call_args.kwargs
        assert url == "http://127.0.0.1:8123/v1/audio/speech"
        assert kwargs["json"] == {
            "model": "xtts-v2",
            "input": "Hello world",
            "voice": "peach",
            "response_format": "mp3",
            "language": "en",
        }
        assert kwargs["timeout"] == 12
        assert kwargs["headers"]["Authorization"] == "Bearer local-secret"

    def test_endpoint_override_and_output_format_from_extension(self, tmp_path):
        from tools.tts_tool import _generate_local_http_tts

        response = MagicMock()
        response.status_code = 200
        response.content = b"opus"
        response.headers = {"content-type": "audio/ogg"}
        response.raise_for_status = MagicMock()

        config = {
            "local_http": {
                "endpoint": "http://localhost:9000/synthesize",
                "model": "vibevoice",
                "voice": "peach",
            }
        }

        with patch("requests.post", return_value=response) as mock_post:
            _generate_local_http_tts("Hi", str(tmp_path / "out.ogg"), config)

        assert mock_post.call_args.args[0] == "http://localhost:9000/synthesize"
        assert mock_post.call_args.kwargs["json"]["response_format"] == "opus"
        assert "Authorization" not in mock_post.call_args.kwargs["headers"]

    def test_json_error_is_sanitized(self, tmp_path):
        from tools.tts_tool import _generate_local_http_tts

        response = MagicMock()
        response.status_code = 500
        response.content = b'{"error":"secret backend failure"}'
        response.headers = {"content-type": "application/json"}
        response.raise_for_status.side_effect = RuntimeError("secret backend failure")

        with patch("requests.post", return_value=response):
            with pytest.raises(RuntimeError) as exc_info:
                _generate_local_http_tts(
                    "Hello",
                    str(tmp_path / "out.mp3"),
                    {"local_http": {"base_url": "http://localhost:8123/v1"}},
                )

        assert "secret backend failure" not in str(exc_info.value)
        assert "HTTP 500" in str(exc_info.value)


class TestTtsDispatcherLocalHttp:
    def test_dispatcher_routes_to_local_http(self, tmp_path):
        from tools.tts_tool import text_to_speech_tool

        with patch("tools.tts_tool._load_tts_config", return_value={"provider": "local_http"}), \
             patch("tools.tts_tool._generate_local_http_tts") as mock_generate:
            mock_generate.side_effect = lambda text, path, config: tmp_path.joinpath("out.mp3").write_bytes(b"audio") or path
            result = json.loads(text_to_speech_tool("Hello", output_path=str(tmp_path / "out.mp3")))

        assert result["success"] is True
        assert result["provider"] == "local_http"
        mock_generate.assert_called_once()
