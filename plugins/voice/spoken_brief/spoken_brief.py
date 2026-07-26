"""Deterministic spoken-brief extraction for TTS text.

This module intentionally avoids model calls.  It removes machine-heavy tokens
and compresses long technical answers into a short spoken summary suitable for
Discord/Telegram voice, while the original full text still goes to chat.
"""

from __future__ import annotations

import re
from typing import Iterable

_MAX_DIRECT_CHARS = 420
_MAX_BRIEF_CHARS = 360
_MAX_SENTENCES = 2

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]{1,120})`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_PATH_RE = re.compile(
    r"(?<!\w)(?:~?/|/[\w.\-]+|[A-Za-z]:[/\\])(?:[\w .\-]+[/\\])*[\w .\-]+",
)
_HASH_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_CONFIG_KEY_RE = re.compile(r"\b[a-zA-Z_][\w-]*(?:\.[a-zA-Z_][\w-]*){1,}\b")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+] |\d+[.)]\s+)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")

_OUTCOME_PREFIXES = (
    "done", "fixed", "implemented", "added", "updated", "created", "verified",
    "blocked", "failed", "found", "the issue", "root cause", "next", "you need",
)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sanitize_for_speech(text: str) -> str:
    """Turn dense machine text into something reasonable to speak aloud."""
    if not text:
        return ""
    text = _CODE_BLOCK_RE.sub(" code block ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" a link ", text)
    text = _UUID_RE.sub(" an identifier ", text)
    text = _HASH_RE.sub(" a hash ", text)
    text = _PATH_RE.sub(" a file path ", text)

    def _inline(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if not token:
            return ""
        if len(token) > 36 or any(ch in token for ch in "/\\{}[]=:;"):
            return " technical value "
        return token.replace("_", " ").replace("-", " ")

    text = _INLINE_CODE_RE.sub(_inline, text)
    text = _CONFIG_KEY_RE.sub(lambda m: m.group(0).replace(".", " dot "), text)
    text = text.replace("→", " to ").replace("=>", " to ")
    text = re.sub(r"[*_>#|]+", " ", text)
    text = re.sub(r"\s*[-–—]{2,}\s*", "; ", text)
    return _collapse_ws(text)


def _sentence_split(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    return [_collapse_ws(p) for p in parts if _collapse_ws(p)]


def _candidate_lines(text: str) -> Iterable[str]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _HEADING_RE.sub("", line)
        line = _BULLET_RE.sub("", line)
        if line:
            yield line


def _is_high_value(line: str) -> bool:
    lower = line.lower().lstrip()
    return lower.startswith(_OUTCOME_PREFIXES) or any(
        marker in lower for marker in ("root cause", "next step", "what worked", "what failed", "blocker", "risk")
    )


def _truncate(text: str, limit: int = _MAX_BRIEF_CHARS) -> str:
    text = _collapse_ws(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "."


def spoken_brief(text: str) -> str:
    """Return a short TTS brief for a full assistant response."""
    clean = sanitize_for_speech(text)
    if not clean:
        return ""
    if len(clean) <= _MAX_DIRECT_CHARS and " code block " not in clean:
        return clean

    selected: list[str] = []
    for line in _candidate_lines(text):
        if _is_high_value(line):
            sanitized = sanitize_for_speech(line)
            if sanitized and sanitized not in selected:
                selected.append(sanitized)
        if len(selected) >= _MAX_SENTENCES:
            break

    if not selected:
        selected = _sentence_split(clean)[:_MAX_SENTENCES]

    if not selected:
        selected = [clean]

    brief = " ".join(selected)
    if len(brief) > _MAX_BRIEF_CHARS:
        brief = _truncate(brief)
    if len(clean) > len(brief) + 160:
        brief = brief.rstrip(".") + ". I kept the full details in text."
    return brief


def transform_tts_text(response_text: str = "", tts_text: str = "", platform: str = "", **_: object) -> str | None:
    """Plugin hook callback for gateway TTS text transformation."""
    source = response_text or tts_text or ""
    transformed = spoken_brief(source)
    if not transformed:
        return None
    return transformed if transformed != source else None
