import json
from pathlib import Path

import pytest


def test_workspace_candidate_selection_and_effect_delta(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.voice_clone_workspace.get_hermes_home", lambda: tmp_path)
    from tools.voice_clone_workspace import latest_workspace_for_chat, save_workspace, select_candidate, apply_effect_delta

    ws = {
        "id": "w1",
        "chat_key": "agent:main:discord:group:chat-123:user-1",
        "samples": [{"index": i, "ok": True, "path": f"/tmp/{i}.mp3"} for i in range(1, 5)],
        "selected_candidates": [],
        "effect_state": {},
        "created_at": 10,
    }
    save_workspace(ws)

    assert latest_workspace_for_chat("chat-123")["id"] == "w1"

    updated = select_candidate("w1", 2)
    assert updated["selected_candidates"] == [2]
    updated = select_candidate("w1", 4)
    assert updated["selected_candidates"] == [2, 4]
    updated = select_candidate("w1", 2)
    assert updated["selected_candidates"] == [4]

    result = apply_effect_delta("w1", "pitch", 1)
    assert result["before"] == 4
    assert result["after"] == 5
    assert result["workspace"]["effect_state"]["pitch"] == 5


def test_create_workspace_generates_four_samples_with_local_http_stack(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.voice_clone_workspace.get_hermes_home", lambda: tmp_path)
    src = tmp_path / "source.wav"
    src.write_bytes(b"fake wav")

    def fake_convert(source: Path, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ref")

    calls = []
    def fake_synth(text, output_path, voice_name, speaker_dir):
        calls.append((text, output_path, voice_name, speaker_dir))
        output_path.write_bytes(b"mp3")
        return str(output_path)

    monkeypatch.setattr("tools.voice_clone_workspace._convert_reference", fake_convert)
    monkeypatch.setattr("tools.voice_clone_workspace._synthesize_sample", fake_synth)

    from tools.voice_clone_workspace import create_voice_clone_workspace, load_workspace

    ws = create_voice_clone_workspace(str(src), chat_key="agent:main:discord:group:1:2")
    assert ws["status"] == "ready"
    assert len(ws["samples"]) == 4
    assert len(calls) == 4
    assert Path(ws["speaker_wav"]).name == "multi"
    assert all(Path(sample["path"]).exists() for sample in ws["samples"])
    assert load_workspace(ws["id"])["id"] == ws["id"]
