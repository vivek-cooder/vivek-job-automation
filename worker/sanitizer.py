"""Targeted privacy and error message sanitization helpers.

Provides bounded, safe string formatting to prevent PII, tokens,
passwords, and URL query credentials from leaking into console logs
or persistent JSON records.
"""

import re
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

MAX_ERROR_LENGTH = 240
FALLBACK_SAFE_MESSAGE = "Error (details suppressed for privacy)"

# Authorization header with Bearer token
_AUTH_HEADER_PATTERN = re.compile(r"(?i)\b(authorization)\s*([:=])\s*bearer\s+([^\s,;'\"\]}>]+)")

# Standalone Bearer header tokens (e.g., Bearer eyJ...)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+([a-zA-Z0-9_\-\.]+)")

# Common secret assignment patterns (e.g., token=xyz, password: 123)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|bot[_-]?token|"
    r"client[_-]?secret|key|otp|pass(?:word|wd)?|private[_-]?key|secret|session[_-]?id|"
    r"token)\b\s*([:=])\s*(?!bearer\b)([^\s,;'\"\]}>]+)",
)

# Email address patterns
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Phone number patterns (various international / local formats, e.g. +1-555-0199, (555) 012-3456)
_PHONE_PATTERN = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{3,4}\b")

# Embedded HTTP(S) and file URLs with query strings or fragments
_EMBEDDED_URL_PATTERN = re.compile(r"(?i)\b((?:https?|file)://[^\s\"'<>,;]+)")


def strip_url_query_and_fragment(url: Any) -> str:
    """Safely strip credentials, query string, and fragment from a URL."""
    try:
        raw = str(url or "").strip()
        if not raw:
            return ""
        parts = urlsplit(raw)
        # Reconstruct without user/pass credentials, query params, or fragment
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        return ""


def _clean_url_match(match: re.Match) -> str:
    full_url = match.group(1)
    cleaned = strip_url_query_and_fragment(full_url)
    return cleaned or full_url.split("?")[0].split("#")[0]


def sanitize_error_text(
    value: Any,
    max_length: int = MAX_ERROR_LENGTH,
    fallback: str = FALLBACK_SAFE_MESSAGE,
) -> str:
    """Sanitize arbitrary exception messages, logs, or error text.

    Removes URL query parameters, auth tokens, passwords, email addresses,
    and phone numbers while strictly bounding the return length.
    Guaranteed not to raise an exception or leak the raw string on failure.
    """
    if value is None:
        return ""

    try:
        if isinstance(value, Exception):
            raw = f"{type(value).__name__}: {str(value)}"
        else:
            raw = str(value)

        raw = raw.strip()
        if not raw:
            return ""

        # 1. Clean any embedded URLs (strip query strings and fragments)
        cleaned = _EMBEDDED_URL_PATTERN.sub(_clean_url_match, raw)

        # 2. Redact Authorization: Bearer tokens
        cleaned = _AUTH_HEADER_PATTERN.sub(r"\1: Bearer [REDACTED]", cleaned)

        # 3. Redact standalone Bearer tokens
        cleaned = _BEARER_PATTERN.sub(r"\1 [REDACTED]", cleaned)

        # 3. Redact common secret/token assignments
        cleaned = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2[REDACTED]", cleaned)

        # 4. Redact email addresses
        cleaned = _EMAIL_PATTERN.sub("[EMAIL_REDACTED]", cleaned)

        # 5. Redact phone numbers
        cleaned = _PHONE_PATTERN.sub("[PHONE_REDACTED]", cleaned)

        # 6. Normalize excessive whitespace and bound length
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if max_length and len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip() + "..."

        return cleaned or fallback
    except Exception:
        return fallback
