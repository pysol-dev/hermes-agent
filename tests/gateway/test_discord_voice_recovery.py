"""Regression coverage for recovered Discord voice/TTS safeguards."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *a, **k: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5,
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


def _discord_event(guild_id: int = 111, chat_id: str = "222") -> MessageEvent:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="333",
        user_name="user",
        chat_type="channel",
    )
    return MessageEvent(
        text="hi",
        message_type=MessageType.VOICE,
        source=source,
        raw_message=SimpleNamespace(guild_id=guild_id, guild=None),
    )


def _runner_for_voice_reply(adapter, *, guild_id: int | None = 111) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._adapter_for_source = lambda _source: adapter  # type: ignore[method-assign]
    runner._get_guild_id = lambda _event: guild_id  # type: ignore[method-assign]
    runner._reply_anchor_for_event = lambda _event: None  # type: ignore[method-assign]
    runner._thread_metadata_for_source = lambda _source, _anchor: None  # type: ignore[method-assign]
    runner._apply_tts_text_transform = lambda _event, text: text  # type: ignore[method-assign]
    return runner


def _fake_tts(monkeypatch, tmp_path, captured_texts: list[str]):
    monkeypatch.setattr("gateway.run.build_auto_tts_output_path", lambda _platform: str(tmp_path / "reply.mp3"))
    monkeypatch.setattr("tools.tts_tool._strip_markdown_for_tts", lambda text: text)

    def fake_text_to_speech_tool(*, text, output_path, **_kwargs):
        captured_texts.append(text)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as fh:
            fh.write(text.encode("utf-8"))
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_text_to_speech_tool)


@pytest.mark.asyncio
async def test_live_vc_default_chunk_limit_safely_splits_long_reply(monkeypatch, tmp_path):
    captured_texts: list[str] = []
    played_texts: list[str] = []
    _fake_tts(monkeypatch, tmp_path, captured_texts)
    monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: {"provider": "local_http"})
    monkeypatch.setattr("tools.tts_tool._get_provider", lambda _cfg: "local_http")
    monkeypatch.setattr("tools.tts_tool._resolve_max_text_length", lambda _provider, _cfg: 4000)

    async def play_in_voice_channel(_guild_id, path):
        with open(path, encoding="utf-8") as fh:
            played_texts.append(fh.read())
        return True

    adapter = SimpleNamespace(
        is_in_voice_channel=lambda guild_id: guild_id == 111,
        play_in_voice_channel=play_in_voice_channel,
    )
    runner = _runner_for_voice_reply(adapter)
    response = " ".join(f"Sentence {idx} has enough words to matter." for idx in range(80))
    limit = runner._voice_tts_chunk_limit()

    await runner._send_voice_reply(_discord_event(), response)

    assert 320 <= limit <= 400
    assert len(captured_texts) > 1
    assert all(len(text) <= limit for text in captured_texts)
    assert played_texts == captured_texts
    assert " ".join(played_texts) == " ".join(response.split())


@pytest.mark.asyncio
async def test_transform_tts_text_changes_only_spoken_source_before_markdown_strip(monkeypatch, tmp_path):
    captured_texts: list[str] = []
    stripped_inputs: list[str] = []
    monkeypatch.setattr("gateway.run.build_auto_tts_output_path", lambda _platform: str(tmp_path / "reply.mp3"))

    def fake_strip(text):
        stripped_inputs.append(text)
        return text.replace("**", "")

    def fake_tts(*, text, output_path, **_kwargs):
        captured_texts.append(text)
        with open(output_path, "wb") as fh:
            fh.write(text.encode("utf-8"))
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool._strip_markdown_for_tts", fake_strip)
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "invoke_hook", lambda *_a, **_kw: ["**spoken brief**"])
    sent_paths: list[str] = []

    async def send_voice(**kwargs):
        sent_paths.append(kwargs["audio_path"])

    adapter = SimpleNamespace(send_voice=send_voice, is_in_voice_channel=lambda *_a: False)
    runner = _runner_for_voice_reply(adapter, guild_id=None)
    runner._apply_tts_text_transform = GatewayRunner._apply_tts_text_transform.__get__(runner, GatewayRunner)  # type: ignore[method-assign]
    written_response = "**full written response**"

    await runner._send_voice_reply(_discord_event(), written_response)

    assert stripped_inputs == ["spoken brief"]
    assert captured_texts == ["spoken brief"]
    assert written_response == "**full written response**"
    assert len(sent_paths) == 1


@pytest.mark.asyncio
async def test_auto_voice_reply_sanitizes_machine_tokens_without_spoken_brief_plugin(monkeypatch, tmp_path):
    captured_texts: list[str] = []
    _fake_tts(monkeypatch, tmp_path, captured_texts)
    sent_paths: list[str] = []

    async def send_voice(**kwargs):
        sent_paths.append(kwargs["audio_path"])

    adapter = SimpleNamespace(send_voice=send_voice, is_in_voice_channel=lambda *_a: False)
    runner = _runner_for_voice_reply(adapter, guild_id=None)
    response = (
        "Saved `/home/lock/.hermes/config.yaml`, see https://example.com/a/b, "
        "hash 0123456789abcdef0123456789abcdef, code `x = {'secret': 1}`."
    )

    await runner._send_voice_reply(_discord_event(), response)

    assert len(sent_paths) == 1
    assert captured_texts
    spoken = captured_texts[0]
    assert "a file path" in spoken
    assert "a link" in spoken
    assert "a hash" in spoken
    assert "technical value" in spoken
    assert "/home/lock" not in spoken
    assert "https://" not in spoken
    assert "0123456789abcdef" not in spoken
    assert "secret" not in spoken


@pytest.mark.asyncio
async def test_non_vc_concat_failure_sends_valid_chunks_individually(monkeypatch, tmp_path):
    captured_texts: list[str] = []
    sent_payloads: list[bytes] = []
    _fake_tts(monkeypatch, tmp_path, captured_texts)
    monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: {"provider": "local_http"})
    monkeypatch.setattr("tools.tts_tool._get_provider", lambda _cfg: "local_http")
    monkeypatch.setattr("tools.tts_tool._resolve_max_text_length", lambda _provider, _cfg: 4000)
    monkeypatch.setattr(
        "gateway.run.subprocess.run",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stderr="concat failed"),
    )

    async def send_voice(**kwargs):
        with open(kwargs["audio_path"], "rb") as fh:
            sent_payloads.append(fh.read())

    adapter = SimpleNamespace(send_voice=send_voice, is_in_voice_channel=lambda *_a: False)
    runner = _runner_for_voice_reply(adapter, guild_id=None)
    response = " ".join(f"Sentence {idx} has enough words to matter." for idx in range(80))

    await runner._send_voice_reply(_discord_event(), response)

    assert len(captured_texts) > 1
    assert sent_payloads == [text.encode("utf-8") for text in captured_texts]


@pytest.mark.asyncio
async def test_live_vc_failed_chunk_aborts_playback(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run.build_auto_tts_output_path", lambda _platform: str(tmp_path / "reply.mp3"))
    monkeypatch.setattr("tools.tts_tool._strip_markdown_for_tts", lambda text: text)
    monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: {"provider": "local_http"})
    monkeypatch.setattr("tools.tts_tool._get_provider", lambda _cfg: "local_http")
    monkeypatch.setattr("tools.tts_tool._resolve_max_text_length", lambda _provider, _cfg: 4000)
    calls = 0
    played: list[str] = []

    def fake_tts(*, text, output_path, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return json.dumps({"success": False, "error": "provider refused chunk"})
        with open(output_path, "wb") as fh:
            fh.write(text.encode("utf-8"))
        return json.dumps({"success": True, "file_path": output_path})

    async def play_in_voice_channel(_guild_id, path):
        with open(path, encoding="utf-8") as fh:
            played.append(fh.read())

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    adapter = SimpleNamespace(
        is_in_voice_channel=lambda guild_id: guild_id == 111,
        play_in_voice_channel=play_in_voice_channel,
    )
    runner = _runner_for_voice_reply(adapter)
    response = " ".join(f"Sentence {idx} has enough words to matter." for idx in range(80))

    await runner._send_voice_reply(_discord_event(), response)

    assert calls == 2
    assert len(played) == 1


@pytest.mark.asyncio
async def test_voice_ack_is_non_blocking_and_dispatch_marks_busy_until_done(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._voice_mode = {}
    runner._discord_voice_busy_guilds = {}
    runner._background_tasks = set()
    runner._is_user_authorized = lambda _source: True  # type: ignore[method-assign]
    runner._is_duplicate_voice_transcript = lambda *_a: False  # type: ignore[method-assign]
    runner._save_voice_modes = lambda: None  # type: ignore[method-assign]
    started_ack = asyncio.Event()
    release_ack = asyncio.Event()

    async def slow_failing_ack(_guild_id, _spoken_text):
        started_ack.set()
        await release_ack.wait()
        raise RuntimeError("ack failed after dispatch")

    runner._play_discord_voice_ack = slow_failing_ack  # type: ignore[method-assign]
    channel = SimpleNamespace(send=AsyncMock())

    async def handle_message(_event):
        assert runner._is_discord_voice_guild_busy(111)

    adapter = SimpleNamespace(
        _voice_text_channels={111: 222},
        _voice_sources={},
        _client=SimpleNamespace(get_channel=lambda _id: channel),
        handle_message=AsyncMock(side_effect=handle_message),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    await runner._handle_voice_channel_input(111, 333, "please run the long check")
    await asyncio.sleep(0)

    adapter.handle_message.assert_awaited_once()
    assert started_ack.is_set()
    assert not runner._is_discord_voice_guild_busy(111)
    assert len(runner._background_tasks) == 1
    release_ack.set()
    await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert runner._background_tasks == set()


@pytest.mark.asyncio
async def test_voice_ack_awaits_async_status_without_blocking_dispatch():
    runner = object.__new__(GatewayRunner)
    runner._voice_mode = {}
    runner._discord_voice_busy_guilds = {}
    runner._background_tasks = set()
    runner._is_user_authorized = lambda _source: True  # type: ignore[method-assign]
    runner._is_duplicate_voice_transcript = lambda *_a: False  # type: ignore[method-assign]
    runner._save_voice_modes = lambda: None  # type: ignore[method-assign]
    status_started = asyncio.Event()
    release_status = asyncio.Event()
    dispatch_reached = asyncio.Event()
    status_calls: list[int] = []

    async def is_in_voice_channel(guild_id):
        status_calls.append(guild_id)
        status_started.set()
        await release_status.wait()
        return False

    async def handle_message(_event):
        dispatch_reached.set()

    play_in_voice_channel = MagicMock()
    channel = SimpleNamespace(send=AsyncMock())
    adapter = SimpleNamespace(
        _voice_text_channels={111: 222},
        _voice_sources={},
        _client=SimpleNamespace(get_channel=lambda _id: channel),
        handle_message=AsyncMock(side_effect=handle_message),
        is_in_voice_channel=is_in_voice_channel,
        play_in_voice_channel=play_in_voice_channel,
    )
    runner.adapters = {Platform.DISCORD: adapter}

    handle_task = asyncio.create_task(
        runner._handle_voice_channel_input(111, 333, "please check status")
    )

    await asyncio.wait_for(dispatch_reached.wait(), timeout=1)
    await asyncio.wait_for(status_started.wait(), timeout=1)
    assert status_calls == [111]
    assert not handle_task.done() or handle_task.exception() is None

    release_status.set()
    await handle_task
    await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)

    adapter.handle_message.assert_awaited_once()
    play_in_voice_channel.assert_not_called()
    assert runner._background_tasks == set()


def test_discord_voice_busy_refcount_survives_overlapping_turns():
    runner = object.__new__(GatewayRunner)
    runner._discord_voice_busy_guilds = {}

    runner._mark_discord_voice_guild_busy(111)
    runner._mark_discord_voice_guild_busy(111)
    runner._unmark_discord_voice_guild_busy(111)

    assert runner._is_discord_voice_guild_busy(111) is True

    runner._unmark_discord_voice_guild_busy(111)

    assert runner._is_discord_voice_guild_busy(111) is False
    assert runner._discord_voice_busy_guilds == {}


@pytest.mark.asyncio
async def test_discord_inactivity_timeout_defers_when_voice_turn_busy(monkeypatch):
    _ensure_discord_mock()
    from plugins.platforms.discord.adapter import DiscordAdapter
    import plugins.platforms.discord.adapter as discord_adapter

    adapter = object.__new__(DiscordAdapter)
    adapter._voice_timeout_tasks = {}
    adapter._voice_text_channels = {111: 222}
    adapter._voice_disconnect_while_busy = False
    adapter._voice_busy_getter = lambda guild_id: guild_id == 111
    adapter._voice_mode_getter = lambda _chat_id: "all"
    adapter._reset_voice_timeout = MagicMock()
    adapter.leave_voice_channel = AsyncMock()
    adapter._on_voice_disconnect = None
    adapter._client = None
    monkeypatch.setattr(discord_adapter.asyncio, "sleep", AsyncMock())

    await adapter._voice_timeout_handler(111, timeout=1)

    adapter.leave_voice_channel.assert_not_awaited()
    adapter._reset_voice_timeout.assert_called_once_with(111)


def test_discord_busy_timeout_config_defaults_and_false_override():
    _ensure_discord_mock()
    from plugins.platforms.discord.adapter import DiscordAdapter

    default_adapter = object.__new__(DiscordAdapter)
    default_adapter.config = PlatformConfig(enabled=True, extra={})
    assert default_adapter._load_voice_disconnect_while_busy() is True

    deferred_adapter = object.__new__(DiscordAdapter)
    deferred_adapter.config = PlatformConfig(
        enabled=True,
        extra={"voice_channel_inactivity_timeout": {"disconnect_while_busy": False}},
    )
    assert deferred_adapter._load_voice_disconnect_while_busy() is False
