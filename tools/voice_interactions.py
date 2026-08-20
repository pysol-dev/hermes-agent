"""Voice interaction policy helpers for gateway voice UX.

These helpers are deterministic and local-only.  They generate short text that
calling gateway code can route to the active voice channel through the existing
local TTS path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_PATH_RE = re.compile(
    r"(?<!\w)(?:(?:~[/\\]|/|[A-Za-z]:[/\\])(?:[^\s`'\")\]}<>,;]+[/\\]?)+)",
)
_SPEECH_FILE_EXTENSIONS = (
    "wav", "mp3", "ogg", "py", "sh", "yaml", "yml", "json", "toml", "log", "db",
)
_BARE_FILENAME_RE = re.compile(
    r"(?<![\w/\\.-])(?P<name>[A-Za-z0-9_.-]+)\."
    r"(?P<ext>" + "|".join(_SPEECH_FILE_EXTENSIONS) + r")\b",
    re.IGNORECASE,
)
_HASH_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_INLINE_CODE_RE = re.compile(r"`([^`]{1,160})`")
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_CONFIG_KEY_RE = re.compile(r"\b[a-zA-Z_][\w-]*(?:\.[a-zA-Z_][\w-]*){1,}\b")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")


class VoiceSurface(str, Enum):
    VOICE_CHANNEL = "voice_channel"
    TEXT_CHAT = "text_chat"
    BOTH = "both"


@dataclass(frozen=True)
class TurnStartConfirmation:
    spoken_text: str
    heard_text: str
    surface: VoiceSurface = VoiceSurface.VOICE_CHANNEL


@dataclass(frozen=True)
class PermissionPrompt:
    spoken_text: str
    text_text: str
    action: str
    risk: str = ""
    approval_options: tuple[str, ...] = ("approve", "deny")
    surface: VoiceSurface = VoiceSurface.BOTH
    metadata: dict[str, str] = field(default_factory=dict)


_FILLER_PREFIXES = ("um", "uh", "hey", "okay", "ok", "so")


def _collapse_spaces(text: str) -> str:
    collapsed = " ".join(str(text or "").split())
    return re.sub(r"\s+([,.;:!?])", r"\1", collapsed)


def sanitize_for_speech(text: str) -> str:
    """Replace machine-heavy fragments with speakable labels."""
    if not text:
        return ""
    text = _CODE_BLOCK_RE.sub(" code block ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" a link ", text)
    text = _UUID_RE.sub(" an identifier ", text)
    text = _HASH_RE.sub(" a hash ", text)
    text = _PATH_RE.sub(" a file path ", text)

    def _filename(match: re.Match[str]) -> str:
        ext = match.group("ext").lower()
        if ext in {"wav", "mp3", "ogg"}:
            return " an audio file "
        if ext in {"py", "sh"}:
            return " a script file "
        if ext in {"yaml", "yml", "json", "toml"}:
            return " a config file "
        return " a data file "

    text = _BARE_FILENAME_RE.sub(_filename, text)

    def _inline(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if not token:
            return ""
        if token in {"a file path", "an audio file", "a script file", "a config file", "a data file"}:
            return f" {token} "
        if len(token) > 36 or any(ch in token for ch in "/\\{}[]=:;"):
            return " technical value "
        return token.replace("_", " ").replace("-", " ")

    text = _INLINE_CODE_RE.sub(_inline, text)
    text = _CONFIG_KEY_RE.sub(lambda m: m.group(0).replace(".", " dot "), text)
    text = text.replace("→", " to ").replace("=>", " to ")
    text = re.sub(r"[*_>#|]+", " ", text)
    text = re.sub(r"\s*[-–—]{2,}\s*", "; ", text)
    return _collapse_spaces(text)


def _strip_addressing(text: str, assistant_names: Sequence[str] = ()) -> str:
    heard = _collapse_spaces(text)
    if not heard:
        return ""
    lowered = heard.lower()
    names = [n.lower().strip(" ,.:;!?-") for n in assistant_names if n]
    for name in names:
        for prefix in (name, f"{name},", f"{name}:"):
            if lowered.startswith(prefix):
                return heard[len(prefix):].lstrip(" ,.:;!?-")
    return heard


def concise_heard_text(
    transcript: str,
    *,
    assistant_names: Sequence[str] = (),
    max_chars: int = 180,
) -> str:
    heard = _strip_addressing(transcript, assistant_names)
    heard = sanitize_for_speech(heard)
    heard = _collapse_spaces(heard).strip(" ,")
    words = heard.split()
    while words and words[0].lower().strip(" ,.:;!?-") in _FILLER_PREFIXES:
        words.pop(0)
    heard = " ".join(words)
    if len(heard) <= max_chars:
        return heard
    cut = heard[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"


def build_turn_start_confirmation(
    transcript: str,
    *,
    assistant_names: Sequence[str] = (),
    prefix: str = "I heard you.",
    max_heard_chars: int = 180,
) -> TurnStartConfirmation | None:
    heard = concise_heard_text(
        transcript,
        assistant_names=assistant_names,
        max_chars=max_heard_chars,
    )
    if not heard:
        return None
    # Spoken turn-start confirmations should acknowledge receipt without
    # parroting the transcript. The sanitized heard_text stays available for
    # logs/tests/debugging, but VC should follow normal conversational-AI
    # convention: brief acknowledgement, then work.
    spoken = f"{prefix} I’ll work on that now."
    return TurnStartConfirmation(spoken_text=spoken, heard_text=heard)


def _join_items(items: Iterable[str], *, limit: int = 3) -> str:
    clean = [sanitize_for_speech(x).strip(" .") for x in items if str(x or "").strip()]
    clean = [x for x in clean if x]
    if not clean:
        return ""
    shown = clean[:limit]
    if len(clean) > limit:
        shown.append(f"{len(clean) - limit} more item{'s' if len(clean) - limit != 1 else ''}")
    if len(shown) == 1:
        return shown[0]
    if len(shown) == 2:
        return f"{shown[0]} and {shown[1]}"
    return ", ".join(shown[:-1]) + ", and " + shown[-1]


def build_permission_prompt(
    action: str,
    *,
    reason: str = "",
    risk: str = "",
    targets: Sequence[str] = (),
    approval_options: Sequence[str] = ("approve", "deny"),
    request_id: str | None = None,
) -> PermissionPrompt:
    clean_action = sanitize_for_speech(action).strip(" .") or "take an action"
    clean_reason = sanitize_for_speech(reason).strip(" .")
    clean_risk = sanitize_for_speech(risk).strip(" .")
    target_text = _join_items(targets)

    # Keep spoken approvals deliberately short and generic. The full command,
    # exact target, and detailed risk belong in the canonical text/buttons
    # prompt; reading shell content aloud is noisy and can leak unsafe or
    # unhelpful machine text into VC.
    spoken_bits = [f"I need your permission to {clean_action}"]
    if clean_risk:
        spoken_bits.append(f"Risk: {clean_risk}")
    spoken_bits.append("Please approve or deny it in chat")
    spoken = ". ".join(spoken_bits) + "."

    text_lines = [f"Permission needed: {clean_action}."]
    if clean_reason:
        text_lines.append(f"Reason: {clean_reason}.")
    if target_text:
        text_lines.append(f"Targets: {target_text}.")
    if clean_risk:
        text_lines.append(f"Risk: {clean_risk}.")
    if request_id:
        text_lines.append(f"Request ID: {request_id}.")
    text_lines.append("Reply with approve or deny.")

    return PermissionPrompt(
        spoken_text=spoken,
        text_text="\n".join(text_lines),
        action=clean_action,
        risk=clean_risk,
        approval_options=tuple(approval_options),
        metadata={"request_id": request_id or ""},
    )
