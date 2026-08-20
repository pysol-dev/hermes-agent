"""Local-first media curation for voice-clone audition workspaces.

This module deliberately uses only local files plus ffmpeg/ffprobe.  Optional
speaker/VAD/enhancement stacks can be layered on later through capability
checks, but absence of those packages must never block the safe fallback path.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
import uuid
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:
        return Path.home() / ".hermes"

AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".webm", ".aiff", ".aif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
AUDIO_MIME_PREFIXES = ("audio/",)
VIDEO_MIME_PREFIXES = ("video/",)
FFMPEG_TIMEOUT_SECONDS = 120
FFPROBE_TIMEOUT_SECONDS = 30
REFERENCE_SAMPLE_RATE = 24000
MAX_FALLBACK_SEGMENTS = 4
FALLBACK_SEGMENT_SECONDS = 18.0


def slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip().lower()).strip("-._")
    return text[:48] or "voice-clone"


def workspace_root() -> Path:
    return get_hermes_home() / "voice_clone_workspaces"


def workspace_json_path(workspace_id: str) -> Path:
    return workspace_root() / workspace_id / "workspace.json"


def manifest_path(root: Path) -> Path:
    return root / "manifest" / "workspace.json"


def is_supported_media(path: str | Path, mime_type: str | None = None) -> bool:
    suffix = Path(str(path)).suffix.lower()
    mime = (mime_type or "").lower()
    if suffix in SUPPORTED_EXTENSIONS:
        return True
    return mime.startswith(AUDIO_MIME_PREFIXES) or mime.startswith(VIDEO_MIME_PREFIXES)


def media_kind(path: str | Path, mime_type: str | None = None) -> str:
    suffix = Path(str(path)).suffix.lower()
    mime = (mime_type or "").lower()
    if suffix in VIDEO_EXTENSIONS or mime.startswith(VIDEO_MIME_PREFIXES):
        return "video"
    if suffix in AUDIO_EXTENSIONS or mime.startswith(AUDIO_MIME_PREFIXES):
        return "audio"
    return "unknown"


def _run_json(cmd: list[str], *, timeout: int) -> Dict[str, Any]:
    proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    return json.loads(proc.stdout or "{}")


def _run_ffmpeg(cmd: list[str], *, timeout: int = FFMPEG_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)


def ffprobe_inventory(path: str | Path) -> Dict[str, Any]:
    src = Path(path)
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(src),
    ]
    data = _run_json(cmd, timeout=FFPROBE_TIMEOUT_SECONDS)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    codecs = []
    has_audio = False
    has_video = False
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "")
        codec = str(stream.get("codec_name") or "unknown")
        if codec_type == "audio":
            has_audio = True
        if codec_type == "video":
            has_video = True
        codecs.append({
            "type": codec_type,
            "codec": codec,
            "sample_rate": stream.get("sample_rate"),
            "channels": stream.get("channels"),
            "duration": stream.get("duration"),
        })
    return {
        "path": str(src),
        "size_bytes": src.stat().st_size if src.exists() else None,
        "duration_seconds": duration,
        "has_audio": has_audio,
        "has_video": has_video,
        "container": fmt.get("format_name"),
        "codecs": codecs,
        "raw": data,
    }


def _copy_source(src: Path, root: Path) -> Path:
    source_dir = root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    dest = source_dir / src.name
    if dest.resolve(strict=False) != src.resolve(strict=False):
        shutil.copy2(src, dest)
    return dest


def _convert_to_reference_wav(src: Path, dest: Path, *, trim_seconds: float | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn"]
    if trim_seconds and trim_seconds > 0:
        cmd.extend(["-t", f"{trim_seconds:.3f}"])
    cmd.extend(["-ac", "1", "-ar", str(REFERENCE_SAMPLE_RATE), "-sample_fmt", "s16", str(dest)])
    _run_ffmpeg(cmd)


def _extract_segment(src_wav: Path, dest: Path, start: float, duration: float) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}", "-i", str(src_wav), "-t", f"{duration:.3f}",
        "-ac", "1", "-ar", str(REFERENCE_SAMPLE_RATE), "-sample_fmt", "s16", str(dest),
    ]
    _run_ffmpeg(cmd)


def _parse_target_timestamp(target_hint: str | None) -> float | None:
    if not target_hint:
        return None
    text = target_hint.strip().lower()
    m = re.search(r"(?:at|target|timestamp)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        parts = [int(p) if p is not None else 0 for p in m.groups()]
        if m.group(3) is not None:
            return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
        return float(parts[0] * 60 + parts[1])
    m = re.search(r"(?:at|target|timestamp)?\s*(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b", text)
    if m:
        return float(m.group(1))
    return None


def fallback_segment(work_wav: Path, duration_seconds: float, *, target_hint: str | None = None) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Produce bounded speech-ish clips with ffmpeg only.

    This is not diarization.  It samples bounded windows from the normalized WAV
    and records speaker isolation confidence as unavailable.
    """
    duration = max(0.0, float(duration_seconds or 0.0))
    raw_dir = work_wav.parent.parent / "segments_raw"
    clean_dir = work_wav.parent.parent / "segments_clean"
    target_ts = _parse_target_timestamp(target_hint)
    if duration <= 0:
        starts = [0.0]
    elif target_ts is not None:
        starts = [max(0.0, min(target_ts, max(0.0, duration - 1.0)))]
    else:
        usable = max(1.0, duration - 1.0)
        count = max(1, min(MAX_FALLBACK_SEGMENTS, math.ceil(duration / FALLBACK_SEGMENT_SECONDS)))
        if count == 1:
            starts = [0.0]
        else:
            starts = [round((usable * i) / count, 3) for i in range(count)]
    candidates: List[Dict[str, Any]] = []
    for idx, start in enumerate(starts[:MAX_FALLBACK_SEGMENTS], start=1):
        seg_dur = min(FALLBACK_SEGMENT_SECONDS, max(1.0, duration - start) if duration else FALLBACK_SEGMENT_SECONDS)
        raw = raw_dir / f"candidate_{idx:02d}.wav"
        clean = clean_dir / f"candidate_{idx:02d}.wav"
        _extract_segment(work_wav, raw, start, seg_dur)
        shutil.copy2(raw, clean)
        candidates.append({
            "index": idx,
            "path": str(clean),
            "raw_path": str(raw),
            "start_seconds": start,
            "duration_seconds": seg_dur,
            "method": "ffmpeg_fixed_window_fallback",
            "speaker_isolation_confidence": None,
            "speaker_isolation_note": "Unavailable: fallback path does not perform diarization or target-speaker isolation.",
        })
    report = {
        "method": "ffmpeg_fixed_window_fallback",
        "speaker_isolation_confidence": None,
        "target_hint": target_hint,
        "target_timestamp_seconds": target_ts,
        "note": "Segments are bounded reference candidates only; user should choose a target candidate when ambiguous.",
    }
    return candidates, report


def analyze_effect_signature(inventory: Dict[str, Any]) -> Dict[str, Any]:
    """Conservative local heuristic metadata; never asserts recognition."""
    suggestions: list[str] = []
    codecs = inventory.get("codecs") or []
    audio = next((c for c in codecs if c.get("type") == "audio"), {})
    sr = 0
    try:
        sr = int(audio.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sr = 0
    codec = str(audio.get("codec") or "").lower()
    if sr and sr <= 24000:
        suggestions.append("probable_band_limiting")
    if codec in {"opus", "vorbis", "mp3", "aac"}:
        suggestions.append("lossy_codec_texture_possible")
    return {
        "label": "heuristic_suggestions_only",
        "confidence": "low",
        "suggestions": suggestions,
        "editable_post_tts_preset_suggestions": {
            "brightness": 3 if "probable_band_limiting" in suggestions else 4,
            "echo": 2,
            "chorus": 3,
            "synth": 6,
        },
        "note": "Effects are suggested for post-TTS audition presets only and are not baked into curated source references.",
    }


def optional_capabilities() -> Dict[str, Dict[str, Any]]:
    caps: Dict[str, Dict[str, Any]] = {}
    for name, role in {
        "demucs": "source separation",
        "faster_whisper": "local speech analysis",
        "noisereduce": "noise reduction",
        "resemblyzer": "speaker embedding analysis",
        "speechbrain": "speaker and VAD analysis",
    }.items():
        try:
            available = find_spec(name) is not None
        except (ImportError, AttributeError, ValueError):
            available = False
        caps[name] = {
            "available": available,
            "role": role,
            "reason": "detected locally; fallback remains available" if available else "not installed; using local ffmpeg fallback",
        }
    return caps


def create_curated_workspace(
    source_media: str,
    *,
    chat_key: str,
    requested_name: str | None = None,
    target_hint: str | None = None,
    mime_type: str | None = None,
) -> Dict[str, Any]:
    src = Path(source_media).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"source media not found: {source_media}")
    if not is_supported_media(src, mime_type):
        raise ValueError(f"unsupported media for voice cloning: {src.suffix or mime_type or src}")

    now = int(time.time())
    stem = slug(requested_name or src.stem)
    wid = f"{now}-{stem}-{uuid.uuid4().hex[:6]}"
    root = workspace_root() / wid
    for rel in ("source", "audit", "work_wav", "segments_raw", "segments_clean", "refs", "effects", "tests", "manifest"):
        (root / rel).mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    source_copy = _copy_source(src, root)
    steps: list[dict[str, Any]] = [{"step": "copy_source", "input": str(src), "output": str(source_copy)}]
    try:
        inventory = ffprobe_inventory(source_copy)
    except Exception as exc:
        inventory = {"path": str(source_copy), "errors": [str(exc)], "has_audio": False, "duration_seconds": 0.0, "codecs": []}
        errors.append(f"ffprobe: {exc}")
    if not inventory.get("has_audio"):
        raise ValueError("media has no audio stream ffmpeg can curate locally")

    kind = media_kind(source_copy, mime_type)
    work_wav = root / "work_wav" / "source_24k_mono.wav"
    _convert_to_reference_wav(source_copy, work_wav)
    steps.append({"step": "extract_audio" if kind == "video" else "convert_audio", "input": str(source_copy), "output": str(work_wav), "format": "24kHz mono PCM s16 WAV"})

    duration = float(inventory.get("duration_seconds") or 0.0)
    candidates, segmentation = fallback_segment(work_wav, duration, target_hint=target_hint)
    steps.append({"step": "fallback_segmentation", "candidates": len(candidates), "method": segmentation["method"]})

    selected_source_candidate: int | None = None
    selection_state = "none"
    if len(candidates) == 1:
        selected_source_candidate = int(candidates[0]["index"])
        selection_state = "provisional"
    elif segmentation.get("target_timestamp_seconds") is not None and candidates:
        selected_source_candidate = int(candidates[0]["index"])
        selection_state = "selected_from_timestamp_hint"
    elif candidates:
        selection_state = "ambiguous_requires_user_selection"

    selected_ref = None
    if selected_source_candidate is not None:
        cand = candidates[selected_source_candidate - 1]
        refs_multi = root / "refs" / "multi"
        refs_multi.mkdir(parents=True, exist_ok=True)
        selected_ref = refs_multi / "ref_01.wav"
        shutil.copy2(cand["path"], selected_ref)
        steps.append({"step": "promote_candidate_to_reference", "candidate": selected_source_candidate, "output": str(selected_ref), "provisional": selection_state == "provisional"})

    manifest = {
        "id": wid,
        "chat_key": chat_key,
        "name": stem,
        "created_at": now,
        "updated_at": now,
        "root": str(root),
        "layout": {k: str(root / k) for k in ("source", "audit", "work_wav", "segments_raw", "segments_clean", "refs", "effects", "tests", "manifest")},
        "source": {"original_path": str(src), "workspace_copy": str(source_copy), "kind": kind, "mime_type": mime_type},
        "inventory": inventory,
        "processing_steps": steps,
        "segmentation": segmentation,
        "source_candidates": candidates,
        "selected_source_candidate": selected_source_candidate,
        "target_selection": {
            "state": selection_state,
            "hint": target_hint,
            "message": "Choose a source candidate before promotion/audition." if selection_state == "ambiguous_requires_user_selection" else "Reference is provisional until user confirms." if selection_state == "provisional" else "Target candidate selected.",
        },
        "reference": {"path": str(selected_ref) if selected_ref else None, "sample_rate": REFERENCE_SAMPLE_RATE, "channels": 1, "pcm": "s16le"},
        "optional_capabilities": optional_capabilities(),
        "effect_signature": analyze_effect_signature(inventory),
        "errors": errors,
    }
    manifest_path(root).write_text(json.dumps(manifest, indent=2) + "\n")
    (root / "audit" / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    return manifest


def load_manifest(workspace_id: str) -> Dict[str, Any]:
    path = workspace_root() / workspace_id / "manifest" / "workspace.json"
    return json.loads(path.read_text())


def save_manifest(manifest: Dict[str, Any]) -> None:
    manifest["updated_at"] = int(time.time())
    path = manifest_path(Path(str(manifest["root"])))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def select_source_candidate(workspace_id: str, index: int) -> Dict[str, Any]:
    manifest = load_manifest(workspace_id)
    candidates = manifest.get("source_candidates") or []
    if index < 1 or index > len(candidates):
        raise ValueError(f"candidate {index} is not in workspace {workspace_id}")
    refs_multi = Path(manifest["root"]) / "refs" / "multi"
    refs_multi.mkdir(parents=True, exist_ok=True)
    dest = refs_multi / "ref_01.wav"
    shutil.copy2(candidates[index - 1]["path"], dest)
    manifest["selected_source_candidate"] = index
    manifest["target_selection"] = {"state": "user_selected", "hint": manifest.get("target_selection", {}).get("hint"), "message": "User selected target source candidate."}
    manifest["reference"] = {"path": str(dest), "sample_rate": REFERENCE_SAMPLE_RATE, "channels": 1, "pcm": "s16le"}
    manifest.setdefault("processing_steps", []).append({"step": "user_select_source_candidate", "candidate": index, "output": str(dest)})
    save_manifest(manifest)
    return manifest


def promote_workspace(workspace: Dict[str, Any], name: str | None = None) -> Dict[str, Any]:
    manifest = load_manifest(str(workspace["id"]))
    ref_path = manifest.get("reference", {}).get("path")
    if not ref_path:
        raise ValueError("select a source candidate before promoting a profile")
    profile_name = slug(name or workspace.get("requested_name") or manifest.get("name") or workspace.get("voice_name") or workspace["id"])
    dest = get_hermes_home() / "voice_refs" / profile_name
    if dest.exists():
        profile_name = f"{profile_name}-{uuid.uuid4().hex[:4]}"
        dest = get_hermes_home() / "voice_refs" / profile_name
    (dest / "multi").mkdir(parents=True, exist_ok=False)
    promoted_profile = {"name": profile_name, "path": str(dest), "active": False, "config_modified": False}
    manifest["promoted_profile"] = promoted_profile
    save_manifest(manifest)
    shutil.copy2(ref_path, dest / "multi" / "ref_01.wav")
    shutil.copy2(manifest_path(Path(manifest["root"])), dest / "manifest.json")
    effects_src = Path(manifest["root"]) / "effects" / "active.json"
    if effects_src.exists():
        (dest / "effects").mkdir(parents=True, exist_ok=True)
        shutil.copy2(effects_src, dest / "effects" / "active.json")
    return promoted_profile
