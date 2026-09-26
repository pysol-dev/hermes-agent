"""OpenAI-compatible local HTTP TTS sidecar for :mod:`tools.tts_tool`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from tools.tts_command_provider import _get_provider_section
from tools.tts_tool_providers import _close_response, _read_tts_response_bytes


def _local_http_tts_config(tts_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return normalized config for the local OpenAI-compatible TTS sidecar."""
    cfg = dict(_get_provider_section(tts_config, "local_http"))
    base_url = str(cfg.get("base_url") or "http://127.0.0.1:8020/v1").rstrip("/")
    endpoint = str(cfg.get("endpoint") or "").strip()
    if not endpoint:
        endpoint = f"{base_url}/audio/speech"
    cfg["base_url"] = base_url
    cfg["endpoint"] = endpoint
    cfg.setdefault("model", "xtts-v2")
    cfg.setdefault("voice", "default")
    cfg.setdefault("timeout", 120)
    return cfg


def _local_http_response_format(output_path: str, cfg: Dict[str, Any]) -> str:
    raw = cfg.get("response_format") or Path(output_path).suffix.lstrip(".") or "mp3"
    fmt = str(raw).strip().lower()
    return "opus" if fmt == "ogg" else (fmt or "mp3")


def _generate_local_http_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """Generate TTS via a local OpenAI-compatible ``/audio/speech`` endpoint."""
    import requests

    cfg = _local_http_tts_config(tts_config)
    payload: Dict[str, Any] = {
        "model": str(cfg.get("model") or "xtts-v2"),
        "input": text,
        "voice": str(cfg.get("voice") or "default"),
        "response_format": _local_http_response_format(output_path, cfg),
    }
    if isinstance(extra_body := cfg.get("extra_body"), dict):
        payload.update(extra_body)
    headers: Dict[str, str] = {"Accept": "audio/*"}
    if api_key := str(cfg.get("api_key") or "").strip():
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.post(
        str(cfg["endpoint"]), json=payload, headers=headers,
        timeout=float(cfg.get("timeout") or 120), stream=True,
    )
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        body = _read_tts_response_bytes(
            response, label="local_http TTS error", limit=64 * 1024)
        detail = body.decode("utf-8", errors="replace")[:500] if body else ""
        raise RuntimeError(f"local_http TTS HTTP {status_code}: {detail}".rstrip())
    audio = _read_tts_response_bytes(response, label="local_http TTS")
    content_type = str((getattr(response, "headers", {}) or {}).get("Content-Type") or "").lower()
    allowed_type = content_type.startswith("audio/") or content_type.startswith("application/octet-stream")
    valid_magic = (
        audio.startswith((b"ID3", b"OggS", b"fLaC"))
        or audio.startswith(b"RIFF") and audio[8:12] == b"WAVE"
        or len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0
        or len(audio) >= 12 and audio[4:8] == b"ftyp"
    )
    if not allowed_type or not valid_magic:
        raise RuntimeError(
            f"local_http TTS returned non-audio content ({content_type or 'missing content-type'})")
    Path(output_path).write_bytes(audio)
    return output_path


def _check_local_http_available(tts_config: Dict[str, Any]) -> bool:
    """Probe local sidecar readiness without synthesizing audio."""
    try:
        import requests
    except Exception:
        return False

    cfg = _local_http_tts_config(tts_config)
    parsed = urlparse(str(cfg.get("endpoint") or ""))
    if parsed.scheme not in {"http", "https"}:
        return False
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    candidates = [f"{base_url}/models", f"{base_url}/health", f"{base_url}/"] if base_url else []
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates.extend([f"{root}/health", f"{root}/"])
    headers: Dict[str, str] = {}
    if api_key := str(cfg.get("api_key") or "").strip():
        headers["Authorization"] = f"Bearer {api_key}"

    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            response = requests.get(url, headers=headers or None, timeout=2)
        except Exception:
            continue
        status_code = int(getattr(response, "status_code", 0) or 0)
        _close_response(response)
        if 200 <= status_code < 500:
            return True
    return False
