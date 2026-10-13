"""Durable JSON queue helpers for the local auto-apply worker."""

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_FILE = BASE_DIR / "data" / "application_queue.json"

TERMINAL_STATUSES = {"Submitted", "Skipped", "Failed"}
WAITING_STATUSES = {"WaitingForHuman", "Waiting for user", "NeedsUserInput"}
ACTIVE_STATUSES = {
    "Started", "Processing", "ReadyForSubmission",
    "Ready for manual submission",
}
IN_PROGRESS_STATUSES = ACTIVE_STATUSES | WAITING_STATUSES
MAX_RETRIES = 3


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def canonicalize_url(url):
    """Normalize URL casing/trailing slash while retaining meaningful query data."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme and not parts.netloc:
            return raw.rstrip("/")
        path = parts.path.rstrip("/")
        if parts.path.startswith("/") and not path:
            path = "/"
        return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, parts.query, ""))
    except ValueError:
        return raw.rstrip("/")


def _source(job):
    return normalize_text(job.get("source") or job.get("source_name"))


def _company(job):
    return normalize_text(job.get("company") or job.get("employer"))


def _title(job):
    return normalize_text(job.get("title") or job.get("job_title"))


def get_job_key(job):
    """Return the strongest available stable identity for a job."""
    for name in ("source_job_id", "job_id", "id"):
        value = normalize_text(job.get(name))
        if value:
            source = _source(job)
            return f"id:{source}:{value}" if source else f"id:{value}"
    url = canonicalize_url(job.get("url"))
    if url:
        return f"url:{url}"
    fields = (_source(job), _company(job), _title(job))
    if all(fields):
        return "job:" + "|".join(fields)
    return ""


def get_job_identity_keys(job):
    """Return all usable identities so URL/title duplicates survive ID changes."""
    keys = []
    source = _source(job)
    for name in ("source_job_id", "job_id", "id"):
        value = normalize_text(job.get(name))
        if value:
            key = f"id:{source}:{value}" if source else f"id:{value}"
            if key not in keys:
                keys.append(key)
    url = canonicalize_url(job.get("url"))
    if url:
        keys.append(f"url:{url}")
    fields = (source, _company(job), _title(job))
    if not keys and all(fields):
        keys.append("job:" + "|".join(fields))
    return keys


def validate_job(job):
    if not isinstance(job, dict):
        return False, "JOB_RECORD_INVALID"
    if not _title(job):
        return False, "JOB_TITLE_MISSING"
    url = str(job.get("url") or "").strip()
    if not url:
        return False, "JOB_URL_MISSING"
    if not (url.startswith(("http://", "https://", "file:///"))):
        return False, "JOB_URL_INVALID"
    return True, None


def load_queue(path=QUEUE_FILE):
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def write_queue(jobs, path=QUEUE_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(jobs, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def find_duplicate(job, jobs):
    incoming = set(get_job_identity_keys(job))
    if not incoming:
        return None
    for existing in jobs:
        if incoming.intersection(get_job_identity_keys(existing)):
            return existing
    return None


def find_protected_queue_duplicate(job, jobs):
    """Find another matching queue entry that is submitted, skipped, active, or waiting."""
    incoming = set(get_job_identity_keys(job))
    protected = {"Submitted", "Skipped"} | ACTIVE_STATUSES | WAITING_STATUSES
    for existing in jobs:
        if existing.get("id") == job.get("id"):
            continue
        if existing.get("status") in protected and incoming.intersection(get_job_identity_keys(existing)):
            return existing
    return None


def add_job(job, path=QUEUE_FILE):
    valid, reason = validate_job(job)
    if not valid:
        return False, None, reason
    jobs = load_queue(path)
    existing = find_duplicate(job, jobs)
    if existing:
        # Preserve all local state; only fill absent metadata from the alert.
        existing.setdefault("job_key", get_job_key(existing))
        for key, value in job.items():
            if key not in existing or existing[key] in (None, ""):
                existing[key] = value
        write_queue(jobs, path)
        return False, existing, "DUPLICATE_JOB"
    record = dict(job)
    now = utc_now()
    record["job_key"] = get_job_key(record)
    record.setdefault("status", "Queued")
    record.setdefault("created_at", now)
    record.setdefault("updated_at", now)
    record.setdefault("retry_count", 0)
    jobs.append(record)
    write_queue(jobs, path)
    return True, record, None


def normalize_suitability_score(raw_score):
    """Normalize score to finite float; return 0.0 for missing, None, boolean, NaN, or non-finite values."""
    if raw_score is None:
        return 0.0
    if isinstance(raw_score, bool):
        return 0.0
    try:
        val = float(raw_score)
        if math.isnan(val) or math.isinf(val):
            return 0.0
        return val
    except (TypeError, ValueError):
        return 0.0


def get_next_job(path=QUEUE_FILE):
    eligible = []
    for index, job in enumerate(load_queue(path)):
        if job.get("status") in {"Queued", "Retry"}:
            eligible.append((index, job))
    if not eligible:
        return None
    eligible.sort(
        key=lambda item: (-normalize_suitability_score(item[1].get("suitability_score")), item[0])
    )
    return eligible[0][1]


def get_waiting_job(path=QUEUE_FILE):
    for job in load_queue(path):
        if job.get("status") in WAITING_STATUSES:
            return job
    return None


def mark_job_status(job_id, status, *, reason=None, last_error=None, retry_count=None,
                    metadata=None, path=QUEUE_FILE, job_key=None):
    jobs = load_queue(path)
    now = utc_now()
    updated = None
    for job in jobs:
        if (job_key and get_job_key(job) == job_key) or (not job_key and job.get("id") == job_id):
            # Never regress terminal/waiting states from stale sync/worker updates.
            current = job.get("status")
            if current in TERMINAL_STATUSES and status not in TERMINAL_STATUSES:
                updated = job
                continue
            if current in WAITING_STATUSES and status in {"Queued", "Started"}:
                updated = job
                continue
            job["status"] = status
            job["updated_at"] = now
            if status in ACTIVE_STATUSES and not job.get("started_at"):
                job["started_at"] = now
            if status in TERMINAL_STATUSES:
                job.setdefault("completed_at", now)
            if reason:
                job["failure_reason"] = reason
            if last_error:
                job["last_error"] = last_error
            elif status not in {"Failed", "Retry"}:
                job.pop("failure_reason", None)
                job.pop("last_error", None)
            if retry_count is not None:
                job["retry_count"] = retry_count
            for key, value in (metadata or {}).items():
                if value is None:
                    job.pop(key, None)
                else:
                    job[key] = value
            updated = job
            break
    if updated is not None:
        write_queue(jobs, path)
    return updated


def recover_stale_jobs(path=QUEUE_FILE, *, max_retries=MAX_RETRIES, stale_after_seconds=0, now=None):
    """Move interrupted active work to Retry, or Failed after the retry limit."""
    jobs = load_queue(path)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    changed = []
    for job in jobs:
        if job.get("status") not in ACTIVE_STATUSES:
            continue
        raw = job.get("updated_at") or job.get("started_at")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
            if stamp and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            stamp = None
        if stamp and (now_dt - stamp).total_seconds() < stale_after_seconds:
            continue
        retries = int(job.get("retry_count", 0) or 0) + 1
        job["retry_count"] = retries
        job["updated_at"] = now_dt.isoformat(timespec="seconds")
        job["last_interrupted_status"] = job.get("status")
        if retries >= max_retries:
            job["status"] = "Failed"
            job["failure_reason"] = "MAX_RETRIES_REACHED"
            job["last_error"] = "Worker stopped during active processing; retry limit reached."
            job.setdefault("completed_at", job["updated_at"])
        else:
            job["status"] = "Retry"
            job["failure_reason"] = "WORKER_INTERRUPTED"
            job["last_error"] = "Worker stopped during active processing; queued for retry."
        changed.append(job)
    if changed:
        write_queue(jobs, path)
    return changed
