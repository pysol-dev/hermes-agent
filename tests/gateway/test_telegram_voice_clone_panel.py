from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_constants import get_hermes_home
from plugins.platforms.telegram import adapter as telegram_adapter
from tools import voice_clone_effect_panel, voice_clone_workspace


@pytest.mark.asyncio
async def test_panel_callbacks_bind_to_the_rendered_workspace_and_topic(monkeypatch):
    adapter = object.__new__(telegram_adapter.TelegramAdapter)
    adapter._bot = object()
    adapter._voice_clone_panel_state = {}
    adapter._reply_to_mode = "auto"
    adapter._metadata_thread_id = lambda metadata: metadata.get("thread_id") if metadata else None
    adapter._reply_to_message_id_for_send = lambda *_args, **_kwargs: None
    adapter._thread_kwargs_for_send = lambda *_args, **_kwargs: {}
    adapter._link_preview_kwargs = lambda: {}
    adapter.send_voice = AsyncMock()
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=SimpleNamespace(message_id=42))

    monkeypatch.setattr(telegram_adapter.secrets, "token_hex", lambda _n: "panel123")
    monkeypatch.setattr(
        telegram_adapter, "InlineKeyboardButton",
        lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data))
    monkeypatch.setattr(
        telegram_adapter, "InlineKeyboardMarkup",
        lambda rows: SimpleNamespace(inline_keyboard=rows))
    monkeypatch.setattr(voice_clone_workspace, "render_workspace_text", lambda _workspace: "panel")
    monkeypatch.setattr(voice_clone_effect_panel, "controls_for_buttons", lambda: [])
    monkeypatch.setattr(voice_clone_effect_panel, "current_panel", lambda: {"state": {}})

    workspace = {
        "id": "workspace-old",
        "samples": [],
        "source_candidates": [{"index": 1}],
        "selected_source_candidate": 1,
        "effect_state": {},
    }
    result = await adapter.send_voice_clone_panel(
        "123", metadata={"thread_id": "9"}, workspace=workspace)

    assert result.success is True
    assert adapter._voice_clone_panel_state["panel123"] == {
        "workspace_id": "workspace-old", "chat_id": "123", "thread_id": "9",
        "profile_home": str(get_hermes_home()),
    }
    keyboard = adapter._send_message_with_thread_fallback.await_args.kwargs["reply_markup"]
    callback_data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callback_data == ["vcs:panel123:1", "vcr:panel123", "vcp:panel123"]
    assert all(len(value.encode()) <= 64 for value in callback_data)


@pytest.mark.asyncio
async def test_callback_uses_bound_profile_workspace_and_rejects_other_chat(tmp_path, monkeypatch):
    adapter = object.__new__(telegram_adapter.TelegramAdapter)
    outer_home = str(get_hermes_home())
    profile_home = tmp_path / "secondary"
    profile_home.mkdir()
    adapter._voice_clone_panel_state = {
        "panel123": {
            "workspace_id": "workspace-old", "chat_id": "123", "thread_id": "9",
            "profile_home": str(profile_home),
        }}
    adapter._callback_authorized = AsyncMock(return_value=True)
    adapter._link_preview_kwargs = lambda: {}
    adapter._voice_clone_keyboard = lambda workspace, panel_id: (workspace["id"], panel_id)

    loaded = []
    reset = []
    observed_homes = []

    def load_workspace(workspace_id):
        loaded.append(workspace_id)
        observed_homes.append(str(get_hermes_home()))
        return {"id": workspace_id}

    def reset_workspace(workspace_id):
        reset.append(workspace_id)
        observed_homes.append(str(get_hermes_home()))
        return {"id": workspace_id}

    monkeypatch.setattr(voice_clone_workspace, "load_workspace", load_workspace)
    monkeypatch.setattr(voice_clone_workspace, "reset_workspace_effects", reset_workspace)
    monkeypatch.setattr(voice_clone_workspace, "render_workspace_text", lambda workspace: workspace["id"])

    query = SimpleNamespace(
        answer=AsyncMock(), edit_message_text=AsyncMock(),
        from_user=SimpleNamespace(id="user-1"))
    await adapter._handle_voice_clone_callback(
        query, "vcr:panel123", {"chat_id": "123", "thread_id": "9", "chat_type": "group", "user_name": "U"})

    assert loaded == ["workspace-old"]
    assert reset == ["workspace-old"]
    assert observed_homes == [str(profile_home), str(profile_home)]
    assert str(get_hermes_home()) == outer_home
    query.edit_message_text.assert_awaited_once()

    loaded.clear()
    reset.clear()
    observed_homes.clear()
    query = SimpleNamespace(
        answer=AsyncMock(), edit_message_text=AsyncMock(),
        from_user=SimpleNamespace(id="user-1"))
    await adapter._handle_voice_clone_callback(
        query, "vcr:panel123", {"chat_id": "999", "thread_id": "9", "chat_type": "group", "user_name": "U"})

    assert loaded == []
    assert reset == []
    assert observed_homes == []
    assert "another chat or topic" in query.answer.await_args.kwargs["text"]
    query.edit_message_text.assert_not_awaited()
