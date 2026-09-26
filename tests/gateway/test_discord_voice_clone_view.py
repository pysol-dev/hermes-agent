from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.discord.adapter import VoiceCloneEffectsView
from tools.voice_clone_effect_panel import controls_for_buttons
from tools.voice_clone_workspace import save_workspace


def _persist_workspace(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    workspace_id = "voiceclone-page-test"
    save_workspace(
        {
            "id": workspace_id,
            "chat_key": "agent:main:discord:group:123:user-1",
            "voice_name": "clone-page-test",
            "source_candidates": [{"index": 1}],
            "selected_source_candidate": 1,
            "samples": [
                {"index": 1, "ok": True, "path": "/tmp/sample-1.mp3"},
                {"index": 2, "ok": True, "path": "/tmp/sample-2.mp3"},
            ],
            "selected_candidates": [2],
            "effect_state": {"pitch": 9},
        }
    )
    return workspace_id


def _button(view, custom_id: str):
    return next(button for button in view.children if button.custom_id == custom_id)


def _effect_keys(view) -> set[str]:
    return {
        button.custom_id.split(":")[2]
        for button in view.children
        if button.custom_id.startswith("vcfx:")
    }


def _interaction(user_id: int):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, roles=[]),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_voice_clone_view_pages_every_effect_and_reconstructs_workspace_state(tmp_path, monkeypatch):
    workspace_id = _persist_workspace(tmp_path, monkeypatch)
    expected_effects = {control.key for control in controls_for_buttons()}
    view = VoiceCloneEffectsView(allowed_user_ids={"123"}, workspace_id=workspace_id)

    first_page_effects = _effect_keys(view)
    next_page = _button(view, f"vcpag:{workspace_id}:1")
    interaction = _interaction(123)
    await next_page.callback(interaction)

    second_page_effects = _effect_keys(view)
    assert first_page_effects | second_page_effects == expected_effects
    assert first_page_effects < expected_effects
    assert any(button.custom_id == f"vccand:{workspace_id}:2" for button in view.children)
    _button(view, f"vcreset:{workspace_id}")
    _button(view, f"vcprom:{workspace_id}")
    assert len(view.children) <= 25
    assert all(button.row is None or 0 <= button.row <= 4 for button in view.children)
    assert all(len(button.custom_id) <= 100 for button in view.children)
    interaction.response.edit_message.assert_awaited_once()

    reconstructed = VoiceCloneEffectsView(allowed_user_ids={"123"}, workspace_id=workspace_id)
    sample_two = _button(reconstructed, f"vccand:{workspace_id}:2")
    assert sample_two.label.startswith("✅")
    assert any("Pitch-glitch 9/10" in button.label for button in reconstructed.children)


@pytest.mark.asyncio
async def test_voice_clone_view_rejects_unauthorized_page_change(tmp_path, monkeypatch):
    workspace_id = _persist_workspace(tmp_path, monkeypatch)
    view = VoiceCloneEffectsView(allowed_user_ids={"123"}, workspace_id=workspace_id)
    next_page = _button(view, f"vcpag:{workspace_id}:1")
    interaction = _interaction(999)

    await next_page.callback(interaction)

    assert view.effects_page == 0
    interaction.response.send_message.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_clone_callback_restores_bound_profile_home_a_b_a(tmp_path, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord import adapter as discord_adapter
    from tools import voice_clone_workspace

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    workspace_id = "voiceclone-profile-scope"
    with _profile_runtime_scope(profile_b, {}, hydrate_secrets=False):
        save_workspace(
            {
                "id": workspace_id,
                "chat_key": "agent:secondary:discord:group:123:user-1",
                "voice_name": "profile-scope",
                "source_candidates": [{"index": 1}],
                "selected_source_candidate": 1,
                "samples": [{"index": 1, "ok": True, "path": "/tmp/sample.mp3"}],
                "selected_candidates": [1],
                "effect_state": {"pitch": 9},
            }
        )
        view = VoiceCloneEffectsView(
            allowed_user_ids={"123"}, workspace_id=workspace_id,
            profile_home=str(profile_b),
        )

    observed_homes = []
    original_reset = voice_clone_workspace.reset_workspace_effects

    def recording_reset(value):
        observed_homes.append(str(get_hermes_home()))
        return original_reset(value)

    monkeypatch.setattr(voice_clone_workspace, "reset_workspace_effects", recording_reset)
    discord_color = getattr(discord_adapter.discord, "Color")
    monkeypatch.setattr(discord_color, "blurple", lambda: None, raising=False)
    interaction = _interaction(123)
    with _profile_runtime_scope(profile_a, {}, hydrate_secrets=False):
        assert str(get_hermes_home()) == str(profile_a)
        await _button(view, f"vcreset:{workspace_id}").callback(interaction)
        assert str(get_hermes_home()) == str(profile_a)

    assert observed_homes == [str(profile_b)]
    interaction.response.edit_message.assert_awaited_once()
    with _profile_runtime_scope(profile_b, {}, hydrate_secrets=False):
        assert voice_clone_workspace.load_workspace(workspace_id)["effect_state"]["pitch"] != 9
