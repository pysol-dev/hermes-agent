import json

import yaml

from tools import voice_clone_effect_panel as panel


def _write_config(home, voice="dr-hayden-3"):
    profile = home / "voice_refs" / voice
    (profile / "multi").mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "tts": {
                    "local_http": {
                        "voice": voice,
                        "extra_body": {
                            "speaker_wav": str(profile / "multi"),
                            "effects": True,
                            "effect_preset": "active",
                        },
                    }
                }
            }
        )
    )
    return profile


def test_panel_text_and_buttons_show_current_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile = _write_config(tmp_path)
    (profile / "effects").mkdir(parents=True)
    (profile / "effects" / "panel_state.json").write_text(
        json.dumps({"pitch": 7, "synth": 8, "breath_guard": 10})
    )

    text = panel.render_panel_text()
    assert "Voice: dr-hayden-3" in text
    assert "Pitch-glitch: 7/10" in text
    assert "Synth color: 8/10" in text
    assert "Digital pitch bending" in text
    assert "No slider changes speech speed or inserts pauses" in text

    pitch = panel.CONTROL_BY_KEY["pitch"]
    assert panel.button_labels(pitch, panel.load_state()) == (
        "− Pitch-glitch 7/10",
        "+ Pitch-glitch 7/10",
    )


def test_adjust_control_clamps_and_writes_active_preset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile = _write_config(tmp_path)

    result = panel.adjust_control("pitch", 99)
    assert result["voice"] == "dr-hayden-3"
    assert result["after"] == 10

    state = json.loads((profile / "effects" / "panel_state.json").read_text())
    assert state["pitch"] == 10

    active = json.loads((profile / "effects" / "active.json").read_text())
    assert active["enabled"] is True
    assert active["panel_state"]["pitch"] == 10
    ffmpeg_filter = active["ffmpeg_filter"]
    assert "rubberband=pitch=" in ffmpeg_filter
    assert "acrusher=" in ffmpeg_filter
    assert "aecho=" in ffmpeg_filter
    # Guardrails from the user's cloning feedback: controls must not change
    # cadence with tempo/silence/gate tricks.
    forbidden = ["atempo", "asetrate", "silenceremove", "agate", "adelay"]
    assert not any(token in ffmpeg_filter for token in forbidden)


def test_reset_panel_restores_safe_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile = _write_config(tmp_path)
    panel.adjust_control("pitch", 5)
    panel.adjust_control("breath_guard", -8)

    state = panel.reset_panel()
    assert state == panel.default_state()

    stored = json.loads((profile / "effects" / "panel_state.json").read_text())
    assert stored == panel.default_state()
    active = json.loads((profile / "effects" / "active.json").read_text())
    assert active["panel_state"] == panel.default_state()
