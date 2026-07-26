"""Local voice-clone audition workspace helpers.

This module intentionally keeps the heavy model/tool surface out of the core
agent schema. Gateway slash/natural-language flows call it directly to build a
local XTTS-style reference profile, synthesize four audition samples through the
configured local_http TTS stack, and persist candidate/effect workspace state for
interactive panels.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import yaml

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:
        return Path.home() / ".hermes"

SAMPLE_TEXTS = [
    "This is sample one. The voice should sound clear, natural, and recognizable.",
    "Sample two checks a calmer cadence, with enough detail to hear the speaker identity.",
    "Sample three adds a little more energy while preserving the original voice character.",
    "Sample four is the effects audition anchor. Keep timing natural and the words intelligible.",
]


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip().lower()).strip("-._")
    return text[:48] or "voice-clone"


def workspace_root() -> Path:
    return get_hermes_home() / "voice_clone_workspaces"


def workspace_path(workspace_id: str) -> Path:
    return workspace_root() / workspace_id / "workspace.json"


def load_workspace(workspace_id: str) -> Dict[str, Any]:
    return json.loads(workspace_path(workspace_id).read_text())


def save_workspace(workspace: Dict[str, Any]) -> Path:
    wid = str(workspace["id"])
    path = workspace_path(wid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(workspace, indent=2) + "\n")
    return path


def latest_workspace_for_chat(chat_key: str) -> Dict[str, Any] | None:
    root = workspace_root()
    if not root.exists():
        return None
    best = None
    for p in root.glob("*/workspace.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        stored_key = str(data.get("chat_key") or "")
        # Exact session-key match is preferred. Callback-only platform events
        # may not have the original user/thread session source, so also allow a
        # conservative chat-id substring match (session keys delimit ids with ':').
        if stored_key == chat_key or (chat_key and f":{chat_key}" in stored_key):
            if best is None or data.get("created_at", 0) > best.get("created_at", 0):
                best = data
    return best


def _convert_reference(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-t", "75", "-ac", "1", "-ar", "24000",
        "-sample_fmt", "s16", str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except Exception:
        shutil.copy2(source, dest)


def _load_tts_config() -> Dict[str, Any]:
    cfg_path = get_hermes_home() / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    return (cfg or {}).get("tts", {}) or {}


def _synthesize_sample(text: str, output_path: Path, voice_name: str, speaker_dir: Path) -> str:
    from tools.tts_tool import _generate_local_http_tts

    tts_cfg = _load_tts_config()
    local = dict((tts_cfg.get("local_http") or {}) if isinstance(tts_cfg, dict) else {})
    extra = dict(local.get("extra_body") or {})
    extra["speaker_wav"] = str(speaker_dir)
    # Prefer the configured sidecar/settings, but force the audition through
    # the new reference profile instead of the currently active live voice.
    local["voice"] = voice_name
    local["extra_body"] = extra
    local.setdefault("response_format", "mp3")
    tts_cfg = dict(tts_cfg)
    tts_cfg["local_http"] = local
    _generate_local_http_tts(text, str(output_path), tts_cfg)
    return str(output_path)


def create_voice_clone_workspace(
    source_audio: str,
    *,
    chat_key: str,
    requested_name: str | None = None,
) -> Dict[str, Any]:
    """Create an end-to-end local audition workspace and four samples.

    The "clone" is an XTTS/local_http zero-shot reference profile: the source
    audio is converted into a profile reference set, then the configured local
    TTS sidecar is asked to synthesize four candidate samples with that profile.
    Future stack improvements remain behind the same local_http configuration.
    """
    src = Path(source_audio).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"source audio not found: {source_audio}")

    now = int(time.time())
    stem = _slug(requested_name or src.stem)
    wid = f"{now}-{stem}-{uuid.uuid4().hex[:6]}"
    root = workspace_root() / wid
    source_dir = root / "source"
    refs_multi = root / "profile" / "multi"
    samples_dir = root / "samples"
    source_dir.mkdir(parents=True, exist_ok=True)
    samples_dir.mkdir(parents=True, exist_ok=True)

    source_copy = source_dir / src.name
    shutil.copy2(src, source_copy)
    ref_path = refs_multi / "ref_01.wav"
    _convert_reference(source_copy, ref_path)

    voice_name = f"clone-{stem}-{uuid.uuid4().hex[:4]}"
    samples: List[Dict[str, Any]] = []
    errors: List[str] = []
    for idx, text in enumerate(SAMPLE_TEXTS, start=1):
        out = samples_dir / f"sample_{idx}.mp3"
        try:
            sample_path = _synthesize_sample(text, out, voice_name, refs_multi)
            ok = True
        except Exception as exc:
            ok = False
            sample_path = ""
            errors.append(f"sample {idx}: {exc}")
        samples.append({"index": idx, "text": text, "path": sample_path, "ok": ok})

    workspace: Dict[str, Any] = {
        "id": wid,
        "chat_key": chat_key,
        "voice_name": voice_name,
        "source_audio": str(source_copy),
        "profile_dir": str(refs_multi.parent),
        "speaker_wav": str(refs_multi),
        "samples": samples,
        "selected_candidates": [],
        "effect_state": {},
        "created_at": now,
        "updated_at": now,
        "errors": errors,
        "status": "ready" if any(s.get("ok") for s in samples) else "sample_generation_failed",
    }
    save_workspace(workspace)
    return workspace


def select_candidate(workspace_id: str, index: int, selected: bool | None = None) -> Dict[str, Any]:
    ws = load_workspace(workspace_id)
    valid = {int(s["index"]) for s in ws.get("samples", [])}
    if index not in valid:
        raise ValueError(f"candidate {index} is not in workspace {workspace_id}")
    current = {int(x) for x in ws.get("selected_candidates", [])}
    if selected is None:
        if index in current:
            current.remove(index)
        else:
            current.add(index)
    elif selected:
        current.add(index)
    else:
        current.discard(index)
    ws["selected_candidates"] = sorted(current)
    ws["updated_at"] = int(time.time())
    save_workspace(ws)
    return ws


def apply_effect_delta(workspace_id: str, control: str, delta: int) -> Dict[str, Any]:
    from tools.voice_clone_effect_panel import CONTROL_BY_KEY, default_state, _clamp

    ws = load_workspace(workspace_id)
    state = dict(default_state())
    state.update(ws.get("effect_state") or {})
    if control not in CONTROL_BY_KEY:
        raise ValueError(f"unknown control: {control}")
    spec = CONTROL_BY_KEY[control]
    before = int(state.get(control, spec.default))
    after = _clamp(before + int(delta), spec.minimum, spec.maximum)
    state[control] = after
    ws["effect_state"] = state
    ws["updated_at"] = int(time.time())
    save_workspace(ws)
    return {"workspace": ws, "before": before, "after": after, "control": control}


def render_workspace_text(workspace: Dict[str, Any]) -> str:
    from tools.voice_clone_effect_panel import CONTROLS, default_state

    selected = set(int(x) for x in workspace.get("selected_candidates", []))
    state = dict(default_state())
    state.update(workspace.get("effect_state") or {})
    lines = [
        "🎙 Voice clone audition workspace",
        "",
        "The voice has been cloned into a local reference profile and four audition samples were generated first.",
        "Select any samples as candidates, then effect edits apply to the selected candidate set in this workspace.",
        "",
        "Candidates:",
    ]
    for sample in workspace.get("samples", []):
        idx = int(sample.get("index", 0))
        mark = "✅" if idx in selected else "⬜"
        status = "ready" if sample.get("ok") else "failed"
        lines.append(f"{mark} Sample {idx}: {status}")
    if not selected:
        lines.append("No candidates selected yet — choose one or more sample buttons below.")
    lines.extend(["", "Effect controls:"])
    for c in CONTROLS:
        lines.append(f"• {c.label}: {state.get(c.key, c.default)}/10 — {c.definition}")
    if workspace.get("errors"):
        lines.extend(["", "Generation notes:"])
        for err in workspace.get("errors", [])[:4]:
            lines.append(f"• {err}")
    return "\n".join(lines)
