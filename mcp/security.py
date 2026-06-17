"""Shared redaction, truncation, and validation helpers."""

from __future__ import annotations

import json
import re
from typing import Any


API_KEY_RE = re.compile(r"\bth-[A-Za-z0-9][A-Za-z0-9_\-]{6,}\b")
AUTH_BEARER_RE = re.compile(
    r"(?i)(Authorization\s*:\s*Bearer\s+)([A-Za-z0-9._~+\-/=]{7,})"
)
INLINE_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+\-/=]{7,})")
COOKIE_RE = re.compile(r"(?i)(Cookie\s*:\s*)(.*?)(?=(?:\s+[A-Za-z][A-Za-z -]{0,32}\s*:)|$)")
TOKEN_FIELD_RE = re.compile(
    r'(?i)(["\']?(?:access_token|refresh_token|token)["\']?\s*[:=]\s*["\']?)([^,"\'}\s]+)'
)
SECRET_FIELD_RE = re.compile(
    r'(?i)(["\']?(?:password|secret|api_secret|client_secret|private_key)["\']?\s*[:=]\s*["\']?)([^,"\'}\s]+)'
)
TRACE_ID_RE = re.compile(r"\b(?:trace_id|traceId)=([A-Za-z0-9_\-]{8,64})\b")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


def mask_api_key(value: str) -> str:
    if not value:
        return value
    visible = min(9, len(value))
    return value[:visible] + ("*" * max(0, len(value) - visible))


def mask_sensitive_text(text: Any) -> Any:
    if text is None or not isinstance(text, str):
        return text

    masked = API_KEY_RE.sub(lambda match: mask_api_key(match.group(0)), text)
    masked = AUTH_BEARER_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)[:6]}{'*' * max(0, len(match.group(2)) - 6)}",
        masked,
    )
    masked = INLINE_BEARER_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)[:6]}{'*' * max(0, len(match.group(2)) - 6)}",
        masked,
    )
    masked = COOKIE_RE.sub(lambda match: f"{match.group(1)}[REDACTED_COOKIE]", masked)
    masked = TOKEN_FIELD_RE.sub(lambda match: f"{match.group(1)}[REDACTED_TOKEN]", masked)
    masked = SECRET_FIELD_RE.sub(lambda match: f"{match.group(1)}[REDACTED_SECRET]", masked)
    return masked


def sanitize_value(value: Any, *, max_text_length: int | None = None) -> tuple[Any, bool]:
    truncated = False
    if isinstance(value, str):
        value = mask_sensitive_text(value)
        if max_text_length is not None and len(value) > max_text_length:
            value = value[:max_text_length] + "..."
            truncated = True
        return value, truncated
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            child, child_truncated = sanitize_value(item, max_text_length=max_text_length)
            sanitized[key] = child
            truncated = truncated or child_truncated
        return sanitized, truncated
    if isinstance(value, list):
        sanitized_items = []
        for item in value:
            child, child_truncated = sanitize_value(item, max_text_length=max_text_length)
            sanitized_items.append(child)
            truncated = truncated or child_truncated
        return sanitized_items, truncated
    return value, False


def truncate_text(text: str, max_length: int) -> tuple[str, bool]:
    masked = mask_sensitive_text(text or "")
    if len(masked) <= max_length:
        return masked, False
    return masked[:max_length] + "...", True


def truncate_middle(
    text: str,
    max_length: int,
    head_length: int,
    tail_length: int,
    marker_label: str,
) -> tuple[str, bool, int, int]:
    masked = mask_sensitive_text(text or "")
    original_length = len(masked)
    if original_length <= max_length:
        return masked, False, original_length, 0

    omitted_chars = max(0, original_length - head_length - tail_length)
    marker = f"\n...[{marker_label} truncated, omitted {omitted_chars} chars]...\n"
    truncated = masked[:head_length] + marker + masked[-tail_length:]
    return truncated, True, original_length, omitted_chars


def truncate_chunk(text: str, max_length: int = 1000) -> tuple[str, bool]:
    return truncate_text(text, max_length)


def extract_trace_id(text: str) -> str | None:
    if not text:
        return None
    match = TRACE_ID_RE.search(text)
    return match.group(1) if match else None


def validate_safe_identifier(value: str, field_name: str) -> None:
    if not value or not SAFE_ID_RE.match(value):
        raise ValueError(f"{field_name} contains unsupported characters")


def parse_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def safe_error_detail(exc: Exception) -> str:
    return mask_sensitive_text(str(exc.__class__.__name__) + ": " + str(exc))
