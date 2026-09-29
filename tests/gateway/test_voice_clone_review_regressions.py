"""Behavioral coverage for merge-review voice dispatch/delivery findings."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import _profile_runtime_scope
from gateway.run_inbound import GatewayInboundMixin
from gateway.session import SessionSource
from hermes_constants import get_hermes_home
from tools import voice_clone_workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_inferred_voiceclone_uses_slash_access_and_routed_scope(tmp_path, denied):
    outer, routed = tmp_path / "outer", tmp_path / "routed"
    outer.mkdir()
    routed.mkdir()
    observed = []

    class Runner(GatewayInboundMixin):
        def _check_slash_access(self, source, command):
            assert command == "voiceclone"
            return "denied" if denied else None

        _hm_command_hooks = AsyncMock(return_value=(False, None, None))

        @asynccontextmanager
        async def _async_profile_scope_for_source(self, source):
            with _profile_runtime_scope(routed, {}, hydrate_secrets=False):
                yield

        def _gateway_plain_command_handlers(self):
            return {"voiceclone": self._handle_voiceclone_command}

        async def _handle_voiceclone_command(self, event):
            observed.append(get_hermes_home())
            return "panel created"

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123")
    event = MessageEvent(text="clone this voice", source=source,
                         message_type=MessageType.DOCUMENT, media_urls=["/tmp/reference.wav"])
    with _profile_runtime_scope(outer, {}, hydrate_secrets=False):
        result = await Runner()._hm_dispatch_idle_commands(event, source, "test")
        assert get_hermes_home() == outer
    assert result == (True, "denied" if denied else "panel created")
    assert observed == ([] if denied else [routed])


@pytest.mark.asyncio
async def test_telegram_source_acknowledges_then_delivers_bound_samples(tmp_path, monkeypatch):
    from plugins.platforms.telegram import adapter as tg
    adapter = object.__new__(tg.TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    profile = tmp_path / "profile"
    profile.mkdir()
    adapter._voice_clone_panel_state = {"p": {
        "workspace_id": "w", "chat_id": "123", "thread_id": "9", "profile_home": str(profile)}}
    adapter._callback_authorized = AsyncMock(return_value=True)
    adapter._link_preview_kwargs = lambda: {}
    adapter._voice_clone_keyboard = lambda *_args: None
    order = []
    query = SimpleNamespace(answer=AsyncMock(side_effect=lambda **_kw: order.append("ack")),
                            edit_message_text=AsyncMock(side_effect=lambda **_kw: order.append("edit")))
    workspace = {"id": "w", "samples": [{"ok": True, "path": str(profile / "s.mp3")}]}
    monkeypatch.setattr(voice_clone_workspace, "load_workspace", lambda _id: {"id": "w"})
    monkeypatch.setattr(voice_clone_workspace, "render_workspace_text", lambda _w: "panel")

    def select(_id, _index):
        assert order == ["ack"]
        assert get_hermes_home() == profile
        order.append("generate")
        return workspace

    async def send(chat, path, metadata=None):
        assert chat == "123" and metadata["thread_id"] == "9"
        assert path == str(profile / "s.mp3")
        assert get_hermes_home() == profile
        order.append("deliver")
        return SimpleNamespace(success=True)

    monkeypatch.setattr(voice_clone_workspace, "select_source_candidate", select)
    adapter.send_voice = AsyncMock(side_effect=send)
    await adapter._handle_voice_clone_callback(query, "vcs:p:1", {"chat_id": "123", "thread_id": "9"})
    assert order == ["ack", "generate", "deliver", "edit"]


@pytest.mark.asyncio
async def test_discord_source_defers_then_delivers_before_refresh(tmp_path, monkeypatch):
    from plugins.platforms.discord import adapter as dc
    profile = tmp_path / "profile"
    profile.mkdir()
    audio = profile / "s.mp3"
    audio.write_bytes(b"audition")
    workspace = {"id": "w", "samples": [{"ok": True, "path": str(audio)}]}
    monkeypatch.setattr(voice_clone_workspace, "load_workspace", lambda _id: {"id": "w"})
    monkeypatch.setattr(voice_clone_workspace, "render_workspace_text", lambda _w: "panel")
    monkeypatch.setattr(dc.discord.Color, "blurple", lambda: None, raising=False)
    monkeypatch.setattr(dc.discord, "File", lambda path: SimpleNamespace(path=path, close=lambda: None))
    view = dc.VoiceCloneEffectsView({"123"}, workspace_id="w", profile_home=str(profile))
    order = []
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123, roles=[]),
        response=SimpleNamespace(defer=AsyncMock(side_effect=lambda: order.append("ack")),
                                 edit_message=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k: order.append("deliver"))),
        edit_original_response=AsyncMock(side_effect=lambda **k: order.append("edit")))

    def select(_id, _index):
        assert order == ["ack"]
        assert get_hermes_home() == profile
        order.append("generate")
        return workspace

    monkeypatch.setattr(voice_clone_workspace, "select_source_candidate", select)
    await view._make_source_callback(1)(interaction)
    assert order == ["ack", "generate", "deliver", "edit"]
    assert [file.path for file in interaction.followup.send.await_args.kwargs["files"]] == [str(audio)]
    interaction.response.edit_message.assert_not_awaited()
