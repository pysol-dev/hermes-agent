"""Local voice-clone audition workspace helpers.

Gateway flows use this thin state layer over :mod:`tools.voice_clone_curation`:
media is inventoried/converted/segmented locally, reference WAVs live in the
workspace, and optional audition samples are generated only after a selected or
provisional reference exists.  No function here changes the active Hermes TTS
voice/config.
"""
from __future__ import annotations

import ipaddress
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import yaml

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:
        return Path.home() / ".hermes"

from tools.voice_clone_curation import (
    create_curated_workspace,
    is_supported_media,
    load_manifest,
    optional_capabilities,
    promote_workspace as _promote_curated_workspace,
    save_manifest,
    select_source_candidate as _select_source_candidate,
    slug as _slug,
    workspace_json_path,
    workspace_root,
)

SAMPLE_TEXTS = [
    "This is sample one. The voice should sound clear, natural, and recognizable.",
    "Sample two checks a calmer cadence, with enough detail to hear the speaker identity.",
    "Sample three adds a little more energy while preserving the original voice character.",
    "Sample four is the effects audition anchor. Keep timing natural and the words intelligible.",
]


def workspace_path(workspace_id: str) -> Path:
    return workspace_json_path(workspace_id)


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
        if stored_key == chat_key or (chat_key and f":{chat_key}" in stored_key):
            if best is None or data.get("created_at", 0) > best.get("created_at", 0):
                best = data
    return best


def _load_tts_config() -> Dict[str, Any]:
    cfg_path = get_hermes_home() / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    return (cfg or {}).get("tts", {}) or {}


def _is_loopback_local_http_endpoint(config: Dict[str, Any]) -> bool:
    """Return whether a local_http endpoint is safely bound to this machine."""
    endpoint = str(
        config.get("endpoint")
        or config.get("base_url")
        or "http://127.0.0.1:8020/v1"
    ).strip()
    parsed = urlparse(endpoint)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _synthesize_sample(text: str, output_path: Path, voice_name: str, speaker_dir: Path) -> str:
    from tools.tts_tool import _generate_local_http_tts

    tts_cfg = _load_tts_config()
    local = dict((tts_cfg.get("local_http") or {}) if isinstance(tts_cfg, dict) else {})
    if not _is_loopback_local_http_endpoint(local):
        raise ValueError("voice-clone auditions require a loopback local_http TTS endpoint")
    extra = dict(local.get("extra_body") or {})
    extra["speaker_wav"] = str(speaker_dir)
    local["voice"] = voice_name
    local["extra_body"] = extra
    local.setdefault("response_format", "mp3")
    tts_cfg = dict(tts_cfg)
    tts_cfg["local_http"] = local
    _generate_local_http_tts(text, str(output_path), tts_cfg)
    return str(output_path)


def _manifest_to_workspace(
    manifest: Dict[str, Any],
    requested_name: str | None,
    voice_name: str,
    samples: List[Dict[str, Any]],
    errors: List[str],
) -> Dict[str, Any]:
    stem = _slug(requested_name or manifest.get("name") or manifest["id"])
    selected = manifest.get("selected_source_candidate")
    root = Path(manifest["root"])
    status = "ready" if any(s.get("ok") for s in samples) else "curated_reference_ready"
    if not selected:
        status = "needs_source_candidate_selection"
    return {
        "id": manifest["id"],
        "chat_key": manifest["chat_key"],
        "voice_name": voice_name,
        "requested_name": requested_name or stem,
        "source_audio": manifest.get("source", {}).get("workspace_copy"),
        "profile_dir": str(root / "refs"),
        "speaker_wav": str(root / "refs" / "multi"),
        "samples": samples,
        "source_candidates": manifest.get("source_candidates", []),
        "selected_source_candidate": selected,
        "target_selection": manifest.get("target_selection", {}),
        "selected_candidates": [],
        "effect_state": {},
        "created_at": manifest.get("created_at", int(time.time())),
        "updated_at": int(time.time()),
        "errors": errors + list(manifest.get("errors") or []),
        "status": status,
        "manifest_path": str(root / "manifest" / "workspace.json"),
        "effects_path": str(root / "effects" / "active.json"),
        "promoted_profile": manifest.get("promoted_profile"),
    }


def create_effect_draft_workspace(chat_key: str) -> Dict[str, Any]:
    """Create an inactive, workspace-scoped effect draft with no source media."""
    now = int(time.time())
    wid = f"{now}-effects-{uuid.uuid4().hex[:6]}"
    root = workspace_root() / wid
    for rel in ("source", "audit", "work_wav", "segments_raw", "segments_clean", "refs", "effects", "tests", "manifest"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    baseline: Dict[str, int] = {}
    try:
        from tools.voice_clone_effect_panel import current_panel

        baseline = dict(current_panel().get("state") or {})
    except Exception:
        pass
    manifest = {
        "id": wid,
        "chat_key": chat_key,
        "name": "voice-clone-effects-draft",
        "created_at": now,
        "updated_at": now,
        "root": str(root),
        "layout": {key: str(root / key) for key in ("source", "audit", "work_wav", "segments_raw", "segments_clean", "refs", "effects", "tests", "manifest")},
        "source": {"original_path": None, "workspace_copy": None, "kind": None, "mime_type": None},
        "inventory": {},
        "processing_steps": [{"step": "create_effect_draft", "baseline_loaded": bool(baseline)}],
        "segmentation": {},
        "source_candidates": [],
        "selected_source_candidate": None,
        "target_selection": {"state": "needs_media", "message": "Attach local audio or video before creating an audition reference."},
        "reference": {"path": None, "sample_rate": 24000, "channels": 1, "pcm": "s16le"},
        "optional_capabilities": optional_capabilities(),
        "effect_signature": {"label": "no_source_media", "confidence": "none", "suggestions": []},
        "errors": [],
    }
    save_manifest(manifest)
    workspace = {
        "id": wid,
        "chat_key": chat_key,
        "voice_name": f"clone-effects-{uuid.uuid4().hex[:4]}",
        "requested_name": "voice-clone-effects-draft",
        "source_audio": None,
        "profile_dir": str(root / "refs"),
        "speaker_wav": str(root / "refs" / "multi"),
        "samples": [],
        "source_candidates": [],
        "selected_source_candidate": None,
        "target_selection": manifest["target_selection"],
        "selected_candidates": [],
        "effect_state": baseline,
        "created_at": now,
        "updated_at": now,
        "errors": [],
        "status": "effects_draft",
        "manifest_path": str(root / "manifest" / "workspace.json"),
        "effects_path": str(root / "effects" / "active.json"),
        "promoted_profile": None,
    }
    save_workspace(workspace)
    return workspace


def create_voice_clone_workspace(
    source_audio: str,
    *,
    chat_key: str,
    requested_name: str | None = None,
    target_hint: str | None = None,
    mime_type: str | None = None,
) -> Dict[str, Any]:
    """Create a local curated workspace and optional local_http auditions.

    Audio and video are accepted.  Video is handled by ffmpeg audio extraction.
    If the fallback segmenter finds multiple possible source candidates without
    a target hint, no audition is generated yet; the UI asks the user to choose.
    """
    if not is_supported_media(source_audio, mime_type):
        raise ValueError(f"unsupported media for voice cloning: {source_audio}")
    manifest = create_curated_workspace(
        source_audio,
        chat_key=chat_key,
        requested_name=requested_name,
        target_hint=target_hint,
        mime_type=mime_type,
    )
    samples: List[Dict[str, Any]] = []
    errors: List[str] = []
    speaker_dir = Path(manifest["root"]) / "refs" / "multi"
    reference_ready = bool(manifest.get("reference", {}).get("path"))
    voice_name = f"clone-{_slug(requested_name or manifest.get('name') or Path(source_audio).stem)}-{uuid.uuid4().hex[:4]}"
    if reference_ready:
        samples_dir = Path(manifest["root"]) / "tests"
        samples_dir.mkdir(parents=True, exist_ok=True)
        for idx, text in enumerate(SAMPLE_TEXTS, start=1):
            out = samples_dir / f"sample_{idx}.mp3"
            try:
                sample_path = _synthesize_sample(text, out, voice_name, speaker_dir)
                ok = True
            except Exception as exc:
                ok = False
                sample_path = ""
                errors.append(f"sample {idx}: {exc}")
            samples.append({"index": idx, "text": text, "path": sample_path, "ok": ok})
    workspace = _manifest_to_workspace(manifest, requested_name, voice_name, samples, errors)
    save_workspace(workspace)
    return workspace


def _resynth_after_source_selection(ws: Dict[str, Any]) -> Dict[str, Any]:
    if any(s.get("ok") for s in ws.get("samples", [])):
        return ws
    root = Path(ws["manifest_path"]).parent.parent
    speaker_dir = root / "refs" / "multi"
    samples_dir = root / "tests"
    samples_dir.mkdir(parents=True, exist_ok=True)
    samples: List[Dict[str, Any]] = []
    errors: List[str] = []
    for idx, text in enumerate(SAMPLE_TEXTS, start=1):
        out = samples_dir / f"sample_{idx}.mp3"
        try:
            sample_path = _synthesize_sample(text, out, ws.get("voice_name") or f"clone-{ws['id']}", speaker_dir)
            ok = True
        except Exception as exc:
            ok = False
            sample_path = ""
            errors.append(f"sample {idx}: {exc}")
        samples.append({"index": idx, "text": text, "path": sample_path, "ok": ok})
    ws["samples"] = samples
    ws["errors"] = list(ws.get("errors") or []) + errors
    ws["status"] = "ready" if any(s.get("ok") for s in samples) else "curated_reference_ready"
    ws["updated_at"] = int(time.time())
    save_workspace(ws)
    return ws


def select_source_candidate(workspace_id: str, index: int) -> Dict[str, Any]:
    manifest = _select_source_candidate(workspace_id, index)
    ws = load_workspace(workspace_id)
    ws["selected_source_candidate"] = index
    ws["target_selection"] = manifest.get("target_selection", {})
    ws["speaker_wav"] = str(Path(manifest["root"]) / "refs" / "multi")
    ws["status"] = "curated_reference_ready"
    ws["updated_at"] = int(time.time())
    save_workspace(ws)
    return _resynth_after_source_selection(ws)


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


def _write_workspace_effect_preset(ws: Dict[str, Any], state: Dict[str, int]) -> str:
    from tools.voice_clone_effect_panel import write_active_preset

    root = Path(ws.get("manifest_path", "")).parent.parent if ws.get("manifest_path") else workspace_root() / str(ws["id"])
    path = write_active_preset(state, profile=root, voice=ws.get("voice_name") or str(ws["id"]))
    try:
        manifest = load_manifest(str(ws["id"]))
        manifest["effect_preset"] = {"path": str(path), "workspace_scoped": True, "config_modified": False}
        save_manifest(manifest)
    except Exception:
        pass
    return str(path)


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
    ws["effects_path"] = _write_workspace_effect_preset(ws, state)
    ws["updated_at"] = int(time.time())
    save_workspace(ws)
    return {"workspace": ws, "before": before, "after": after, "control": control}


def reset_workspace_effects(workspace_id: str) -> Dict[str, Any]:
    from tools.voice_clone_effect_panel import default_state

    ws = load_workspace(workspace_id)
    state = default_state()
    ws["effect_state"] = state
    ws["effects_path"] = _write_workspace_effect_preset(ws, state)
    ws["updated_at"] = int(time.time())
    save_workspace(ws)
    return ws


def promote_workspace(workspace_id: str, name: str | None = None) -> Dict[str, Any]:
    ws = load_workspace(workspace_id)
    if not ws.get("selected_source_candidate"):
        raise ValueError("select a source candidate before promoting a profile")
    successful_samples = {
        int(sample["index"])
        for sample in ws.get("samples", [])
        if sample.get("ok") and sample.get("index") is not None
    }
    selected_samples = {int(index) for index in ws.get("selected_candidates", [])}
    if not successful_samples:
        raise ValueError("generate local audition samples before promoting a profile")
    if not successful_samples & selected_samples:
        raise ValueError("select at least one successful audition sample before promoting a profile")
    promoted = _promote_curated_workspace(ws, name=name)
    ws["promoted_profile"] = promoted
    ws["updated_at"] = int(time.time())
    save_workspace(ws)
    return {"workspace": ws, "promoted_profile": promoted}


def render_workspace_text(workspace: Dict[str, Any]) -> str:
    from tools.voice_clone_effect_panel import CONTROLS, default_state

    selected_samples = set(int(x) for x in workspace.get("selected_candidates", []))
    selected_source = workspace.get("selected_source_candidate")
    target = workspace.get("target_selection") or {}
    state = dict(default_state())
    state.update(workspace.get("effect_state") or {})
    lines = [
        "🎙 Voice clone audition workspace",
        "",
        "Local-first: source media stayed on this machine; ffmpeg produced 24 kHz mono PCM reference WAVs.",
        "No active Hermes TTS voice/config was changed.",
        "",
        "Source candidates:",
    ]
    for cand in workspace.get("source_candidates", []):
        idx = int(cand.get("index", 0))
        mark = "✅" if idx == selected_source else "⬜"
        start = float(cand.get("start_seconds") or 0.0)
        dur = float(cand.get("duration_seconds") or 0.0)
        lines.append(f"{mark} Source {idx}: {start:.1f}s–{start + dur:.1f}s (speaker isolation confidence unavailable)")
    if target.get("state") == "ambiguous_requires_user_selection":
        lines.append("Choose the source clip that contains the target speaker before audition/promotion.")
    elif target.get("state") == "provisional":
        lines.append("Using the only detected source clip as a provisional reference; confirm or replace before promotion.")
    elif target.get("state") == "needs_media":
        lines.append("Attach local audio or video to create a source reference and audition before promotion.")
    lines.extend(["", "Audition samples:"])
    for sample in workspace.get("samples", []):
        idx = int(sample.get("index", 0))
        mark = "✅" if idx in selected_samples else "⬜"
        status = "ready" if sample.get("ok") else "failed"
        lines.append(f"{mark} Sample {idx}: {status}")
    if not workspace.get("samples"):
        if target.get("state") == "needs_media":
            lines.append("No audition samples yet — this inactive effect draft has no source media.")
        else:
            lines.append("No audition samples yet — select a source candidate first.")
    elif not selected_samples:
        lines.append("No audition sample selected yet — choose one or more sample buttons below.")
    lines.extend(["", "Effect controls (workspace-scoped):"])
    for c in CONTROLS:
        lines.append(f"• {c.label}: {state.get(c.key, c.default)}/10 — {c.definition}")
    try:
        manifest = load_manifest(str(workspace["id"]))
        sig = manifest.get("effect_signature") or {}
        if sig.get("suggestions"):
            lines.extend(["", "Heuristic effect suggestions (editable, not asserted):"])
            for item in sig.get("suggestions", [])[:4]:
                lines.append(f"• {item}")
    except Exception:
        pass
    if workspace.get("promoted_profile"):
        prof = workspace["promoted_profile"]
        lines.extend(["", f"Promoted inactive profile: {prof.get('name')} at {prof.get('path')} (config not modified)"])
    if workspace.get("errors"):
        lines.extend(["", "Generation notes:"])
        for err in workspace.get("errors", [])[:4]:
            lines.append(f"• {err}")
    return "\n".join(lines)
