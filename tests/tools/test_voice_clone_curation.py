import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import voice_clone_curation as curation
from tools import voice_clone_workspace as workspace


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="local media curation requires ffmpeg and ffprobe",
)


def test_curation_preserves_source_and_promotes_inactive_profile(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setattr(curation, "get_hermes_home", lambda: home)
    source = tmp_path / "source.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
            "-c:a", "pcm_s16le", str(source),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    original_bytes = source.read_bytes()
    config_path = home / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("tts:\n  local_http:\n    voice: live-voice\n")
    config_before = config_path.read_text()

    manifest = curation.create_curated_workspace(str(source), chat_key="agent:main:discord:dm:1")
    root = Path(manifest["root"])

    assert source.read_bytes() == original_bytes
    assert (root / "source" / source.name).read_bytes() == original_bytes
    assert all((root / name).is_dir() for name in ("source", "audit", "work_wav", "segments_raw", "segments_clean", "refs", "effects", "tests", "manifest"))
    assert manifest["inventory"]["has_audio"] is True
    assert manifest["segmentation"]["speaker_isolation_confidence"] is None
    assert isinstance(manifest["optional_capabilities"]["faster_whisper"]["available"], bool)

    reference = Path(manifest["reference"]["path"])
    inventory = curation.ffprobe_inventory(reference)
    audio = next(codec for codec in inventory["codecs"] if codec["type"] == "audio")
    assert int(audio["sample_rate"]) == 24000
    assert audio["channels"] == 1

    ws = {
        "id": manifest["id"],
        "requested_name": "local-test",
        "voice_name": "clone-local-test",
        "manifest_path": str(root / "manifest" / "workspace.json"),
        "samples": [{"index": 1, "ok": True, "path": str(root / "tests" / "sample_1.mp3")}],
        "selected_source_candidate": manifest["selected_source_candidate"],
        "selected_candidates": [1],
        "effect_state": {},
    }
    workspace.save_workspace(ws)
    workspace.apply_effect_delta(manifest["id"], "pitch", 1)
    result = workspace.promote_workspace(manifest["id"])
    promoted = result["promoted_profile"]
    promoted_root = Path(promoted["path"])

    assert promoted["active"] is False
    assert promoted["config_modified"] is False
    assert (promoted_root / "multi" / "ref_01.wav").is_file()
    assert (promoted_root / "effects" / "active.json").is_file()
    assert json.loads((promoted_root / "manifest.json").read_text())["promoted_profile"] == promoted
    assert config_path.read_text() == config_before
