import json
from pathlib import Path

import pytest
import yaml

from tools import voice_clone_curation as curation
from tools import voice_clone_workspace as workspace


@pytest.fixture
def workspace_home(tmp_path, monkeypatch):
    monkeypatch.setattr(curation, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(workspace, "get_hermes_home", lambda: tmp_path)
    return tmp_path


def _curated_manifest(home: Path, workspace_id: str = "w1"):
    root = home / "voice_clone_workspaces" / workspace_id
    reference = root / "refs" / "multi" / "ref_01.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"reference")
    return {
        "id": workspace_id,
        "chat_key": "agent:main:discord:group:chat-123:user-1",
        "name": "source",
        "root": str(root),
        "created_at": 10,
        "source": {"workspace_copy": str(root / "source" / "source.wav")},
        "source_candidates": [{"index": 1, "path": str(reference)}],
        "selected_source_candidate": 1,
        "target_selection": {"state": "provisional"},
        "reference": {"path": str(reference)},
        "errors": [],
    }


def test_workspace_candidate_selection_and_workspace_preset(workspace_home):
    root = workspace_home / "voice_clone_workspaces" / "w1"
    curation.save_manifest({"id": "w1", "root": str(root)})
    ws = {
        "id": "w1",
        "chat_key": "agent:main:discord:group:chat-123:user-1",
        "voice_name": "clone-w1",
        "manifest_path": str(root / "manifest" / "workspace.json"),
        "samples": [{"index": index, "ok": True, "path": f"/tmp/{index}.mp3"} for index in range(1, 5)],
        "selected_source_candidate": 1,
        "selected_candidates": [],
        "effect_state": {},
        "created_at": 10,
    }
    workspace.save_workspace(ws)

    assert workspace.latest_workspace_for_chat("chat-123")["id"] == "w1"

    updated = workspace.select_candidate("w1", 2)
    assert updated["selected_candidates"] == [2]
    updated = workspace.select_candidate("w1", 4)
    assert updated["selected_candidates"] == [2, 4]
    updated = workspace.select_candidate("w1", 2)
    assert updated["selected_candidates"] == [4]

    result = workspace.apply_effect_delta("w1", "pitch", 1)
    assert result["before"] == 4
    assert result["after"] == 5
    assert result["workspace"]["effect_state"]["pitch"] == 5
    effects_path = Path(result["workspace"]["effects_path"])
    assert effects_path == root / "effects" / "active.json"
    assert json.loads(effects_path.read_text())["panel_state"]["pitch"] == 5
    assert not (workspace_home / "config.yaml").exists()


def test_create_workspace_keeps_sample_voice_identity(workspace_home, monkeypatch):
    manifest = _curated_manifest(workspace_home)
    monkeypatch.setattr(workspace, "create_curated_workspace", lambda *_args, **_kwargs: manifest)
    calls = []

    def fake_synth(text, output_path, voice_name, speaker_dir):
        calls.append((text, output_path, voice_name, speaker_dir))
        output_path.write_bytes(b"mp3")
        return str(output_path)

    monkeypatch.setattr(workspace, "_synthesize_sample", fake_synth)

    ws = workspace.create_voice_clone_workspace(
        "source.wav",
        chat_key=manifest["chat_key"],
        requested_name="Example Voice",
    )

    assert ws["status"] == "ready"
    assert len(ws["samples"]) == 4
    assert {call[2] for call in calls} == {ws["voice_name"]}
    assert all(Path(sample["path"]).exists() for sample in ws["samples"])
    assert workspace.load_workspace(ws["id"])["voice_name"] == ws["voice_name"]


def test_create_workspace_refuses_non_loopback_tts(workspace_home, monkeypatch):
    manifest = _curated_manifest(workspace_home)
    monkeypatch.setattr(workspace, "create_curated_workspace", lambda *_args, **_kwargs: manifest)
    (workspace_home / "config.yaml").write_text(
        yaml.safe_dump({"tts": {"local_http": {"base_url": "https://tts.example.test/v1"}}})
    )

    ws = workspace.create_voice_clone_workspace("source.wav", chat_key=manifest["chat_key"])

    assert ws["status"] == "curated_reference_ready"
    assert all(not sample["ok"] for sample in ws["samples"])
    assert all("loopback" in error for error in ws["errors"])


def test_effect_draft_stays_inactive_and_cannot_promote(workspace_home):
    draft = workspace.create_effect_draft_workspace("agent:main:telegram:group:chat-123:user-1")

    assert draft["status"] == "effects_draft"
    assert draft["target_selection"]["state"] == "needs_media"
    result = workspace.apply_effect_delta(draft["id"], "brightness", 1)
    assert Path(result["workspace"]["effects_path"]).is_file()
    assert not (workspace_home / "config.yaml").exists()
    with pytest.raises(ValueError, match="source candidate"):
        workspace.promote_workspace(draft["id"])
