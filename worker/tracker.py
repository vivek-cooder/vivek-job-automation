"""JSON application history with backward-compatible identity matching."""

import json
import os
import tempfile
from pathlib import Path

from worker.job_queue import (
    BASE_DIR, get_job_identity_keys, get_job_key, utc_now,
)

TRACKER_FILE = BASE_DIR / "data" / "applications.json"

SUBMITTED_STATUSES = {"Submitted", "Applied", "Application submitted", "Successfully applied"}
ACTIVE_STATUSES = {
    "Started", "Processing", "WaitingForHuman", "Waiting for user",
    "NeedsUserInput", "ReadyForSubmission", "Ready for manual submission", "Queued",
}


def _read(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write(applications, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(applications, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def load_applications(path=TRACKER_FILE):
    return _read(path)


def _as_job(record):
    return {
        "source_job_id": record.get("source_job_id"),
        "job_id": record.get("job_id"),
        "id": record.get("queue_id"),
        "source": record.get("source"),
        "company": record.get("company"),
        "title": record.get("job_title") or record.get("title"),
        "url": record.get("url"),
    }


def find_application(job, applications=None, path=TRACKER_FILE):
    applications = applications if applications is not None else load_applications(path)
    incoming = set(get_job_identity_keys(job))
    if not incoming:
        return None
    for application in applications:
        if incoming.intersection(get_job_identity_keys(_as_job(application))):
            return application
    return None


def is_duplicate_application(job, applications=None, path=TRACKER_FILE):
    """Treat submitted or currently active matching applications as duplicates."""
    application = find_application(job, applications, path)
    if not application:
        return False, None
    status = application.get("status", "")
    if status in SUBMITTED_STATUSES or status in ACTIVE_STATUSES:
        return True, application
    return False, application


def save_application(
    company, job_title, location, url, source, status="Queued", *,
    job_key=None, queue_id=None, job=None, failure_reason=None,
    retry_count=0, path=TRACKER_FILE,
):
    applications = load_applications(path)
    reference = dict(job or {})
    reference.update({
        "company": company, "title": job_title, "url": url,
        "source": source, "id": queue_id,
    })
    stable_key = job_key or get_job_key(reference)
    existing = find_application(reference, applications)
    now = utc_now()
    if existing:
        # Preserve the historical record and enrich it rather than append a duplicate.
        existing.setdefault("job_key", stable_key)
        if queue_id and not existing.get("queue_id"):
            existing["queue_id"] = queue_id
        existing["updated_at"] = now
        if status:
            existing["status"] = status
        if status in ACTIVE_STATUSES:
            existing["failure_reason"] = None
        if failure_reason:
            existing["failure_reason"] = failure_reason
        return existing
    record = {
        "job_key": stable_key,
        "queue_id": queue_id,
        "company": company,
        "job_title": job_title,
        "location": location,
        "url": url,
        "source": source,
        "status": status,
        "created_at": now,
        "updated_at": now,
        "submitted_at": now if status in SUBMITTED_STATUSES else None,
        "failure_reason": failure_reason,
        "retry_count": retry_count,
    }
    applications.append(record)
    _write(applications, path)
    return record


def update_application_status(job, new_status, *, failure_reason=None, retry_count=None,
                              metadata=None, path=TRACKER_FILE):
    applications = load_applications(path)
    application = find_application(job, applications)
    now = utc_now()
    if application:
        application["status"] = new_status
        application["updated_at"] = now
        if new_status in SUBMITTED_STATUSES:
            application["submitted_at"] = now
            application["failure_reason"] = None
        elif new_status in ACTIVE_STATUSES:
            application["failure_reason"] = None
        elif failure_reason:
            application["failure_reason"] = failure_reason
        if retry_count is not None:
            application["retry_count"] = retry_count
        for key, value in (metadata or {}).items():
            if value is None:
                application.pop(key, None)
            else:
                application[key] = value
        _write(applications, path)
    return application


def update_status(company, job_title, new_status, *, failure_reason=None, path=TRACKER_FILE):
    """Legacy-compatible update; prefer update_application_status with a stable job reference."""
    applications = load_applications(path)
    matches = [a for a in applications if
               str(a.get("company", "")).casefold() == str(company).casefold()
               and str(a.get("job_title", "")).casefold() == str(job_title).casefold()]
    for application in matches:
        application["status"] = new_status
        application["updated_at"] = utc_now()
        if failure_reason:
            application["failure_reason"] = failure_reason
        if new_status in SUBMITTED_STATUSES:
            application["submitted_at"] = application["updated_at"]
    if matches:
        _write(applications, path)
    return matches


def already_applied(company, job_title, *, path=TRACKER_FILE):
    """Legacy title check retained for callers; stable URL/key check is preferred."""
    return any(
        str(a.get("company", "")).casefold() == str(company).casefold()
        and str(a.get("job_title", "")).casefold() == str(job_title).casefold()
        and a.get("status") in SUBMITTED_STATUSES | ACTIVE_STATUSES
        for a in load_applications(path)
    )
