"""Best-effort Telegram notifications using the official Bot HTTP API."""

import json
import os
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10
_unavailable_logged = False
_SECRET_QUERY_PARTS = ("token", "auth", "session", "password", "secret", "otp", "code", "key")


def _log_unavailable_once():
    global _unavailable_logged
    if not _unavailable_logged:
        print("[TELEGRAM] Notifications disabled or not configured.")
        _unavailable_logged = True


def _enabled(value=None):
    raw = os.getenv("TELEGRAM_ENABLED", "false") if value is None else value
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def send_message(text, *, bot_token=None, chat_id=None, enabled=None,
                 timeout=DEFAULT_TIMEOUT_SECONDS):
    """Send text, returning safe structured results that never include credentials."""
    if not _enabled(enabled):
        _log_unavailable_once()
        return {"success": False, "error": "TELEGRAM_DISABLED"}
    token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
    recipient = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not recipient:
        _log_unavailable_once()
        return {"success": False, "error": "TELEGRAM_NOT_CONFIGURED"}

    # The token is required in the Bot API URL. Keep URL/HTTP exception details private.
    endpoint = f"{API_BASE}/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": recipient,
        "text": str(text),
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            code = "TELEGRAM_INVALID_CREDENTIALS"
        elif exc.code == 400:
            code = "TELEGRAM_INVALID_CHAT_ID"
        else:
            code = "TELEGRAM_HTTP_ERROR"
        return {"success": False, "error": code}
    except (TimeoutError, socket.timeout):
        return {"success": False, "error": "TELEGRAM_TIMEOUT"}
    except URLError:
        return {"success": False, "error": "TELEGRAM_CONNECTION_ERROR"}
    except OSError:
        return {"success": False, "error": "TELEGRAM_CONNECTION_ERROR"}
    except (ValueError, json.JSONDecodeError):
        return {"success": False, "error": "TELEGRAM_RESPONSE_ERROR"}

    if not isinstance(result, dict) or result.get("ok") is not True:
        api_code = result.get("error_code") if isinstance(result, dict) else None
        code = {
            401: "TELEGRAM_INVALID_CREDENTIALS",
            400: "TELEGRAM_INVALID_CHAT_ID",
        }.get(api_code, "TELEGRAM_API_ERROR")
        return {"success": False, "error": code}
    details = result.get("result")
    message_id = details.get("message_id") if isinstance(details, dict) else None
    return {"success": True, "message_id": message_id}


def _safe_url(url):
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
            return ""
        if parts.username or parts.password:
            return ""
        safe_query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not any(secret in key.casefold() for secret in _SECRET_QUERY_PARTS)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))
    except ValueError:
        return ""


def _human_reason(reason):
    labels = {
        "CAPTCHA_REQUIRED": "CAPTCHA verification required",
        "OTP_REQUIRED": "OTP verification required",
        "EMAIL_VERIFICATION_REQUIRED": "Email verification required",
        "PHONE_VERIFICATION_REQUIRED": "Phone verification required",
        "LOGIN_APPROVAL_REQUIRED": "Login approval required",
        "SECURITY_CHALLENGE_REQUIRED": "Browser/security verification required",
        "REQUIRED_FIELDS_NEED_USER_INPUT": "Required form information needs your input",
    }
    return labels.get(str(reason), re.sub(r"[_]+", " ", str(reason or "Human action required")).strip())


def send_human_intervention_notification(job, reason, *, status="WaitingForHuman", event_id=None):
    url = _safe_url(job.get("current_url") or job.get("url"))
    lines = [
        "🔔 Human action required",
        "",
        f"🏢 Company: {job.get('company') or 'Unknown'}",
        f"💼 Role: {job.get('title') or job.get('job_title') or 'Unknown'}",
    ]
    if job.get("location"):
        lines.append(f"📍 Location: {job['location']}")
    lines.extend([
        f"⚠️ Reason: {_human_reason(reason)}",
        "",
        "⚠️ Complete this verification directly in the already-open desktop browser window.",
        "Do not attempt to complete verification on a mobile device.",
        "The local worker will resume automatically once it detects completion.",
        f"Job ID: {job.get('id') or job.get('queue_id') or 'unknown'}",
    ])
    if url:
        lines.append(f"Employer reference link (view only; not a remote session): {url}")
    return send_message("\n".join(lines))


def send_application_ready_notification(job):
    lines = [
        "✅ APPLICATION READY",
        "",
        f"Company: {job.get('company') or 'Unknown'}",
        f"Role: {job.get('title') or job.get('job_title') or 'Unknown'}",
        "The form has been filled and validated.",
        "AUTO_SUBMIT is disabled. Manual submission is required.",
        f"Job ID: {job.get('id') or job.get('queue_id') or 'unknown'}",
    ]
    return send_message("\n".join(lines))


def send_application_result_notification(job, status, reason=None):
    submitted = status == "Submitted"
    lines = [
        "✅ APPLICATION SUBMITTED" if submitted else "❌ APPLICATION FAILED",
        "",
        f"Company: {job.get('company') or 'Unknown'}",
        f"Role: {job.get('title') or job.get('job_title') or 'Unknown'}",
    ]
    if reason and not submitted:
        lines.append(f"Reason: {str(reason)[:240]}")
    lines.append(f"Job ID: {job.get('id') or job.get('queue_id') or 'unknown'}")
    return send_message("\n".join(lines))
