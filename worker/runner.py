"""Phase 2 worker with structured form outcomes and resumable human gates."""

import argparse
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

from worker.applicant import load_applicant_profile
from worker.application import apply_to_job
from worker.browser import start_browser
from worker.detector import detect_application_result, detect_verification
from worker.navigation import (
    NavigationRejected, configured_policy, current_page_is_authorized,
    install_page_guard,
)
from worker.job_queue import (
    ACTIVE_STATUSES, MAX_RETRIES, TERMINAL_STATUSES, WAITING_STATUSES,
    find_protected_queue_duplicate, get_job_key, get_next_job,
    get_waiting_job, load_queue, mark_job_status, recover_stale_jobs,
    utc_now, validate_job,
)
from worker.settings import auto_submit_enabled, resolve_auto_submit
from worker.tracker import (
    is_duplicate_application, save_application, update_application_status,
)
from worker import telegram
from worker.sanitizer import sanitize_error_text, strip_url_query_and_fragment

RECOVERY_STALE_SECONDS = 600
MANUAL_RESULT_TIMEOUT_SECONDS = 1800
HUMAN_EVENT_FIELDS = (
    "waiting_event_id", "human_notification_attempted_at",
    "human_notification_sent_at", "human_notification_id",
    "human_notification_error",
)


def _job_label(job):
    return f"{job.get('title') or job.get('job_title') or 'Unknown'} - {job.get('company') or 'Unknown company'}"


def _tracker_job(job):
    company = job.get("company")
    if not company:
        parts = str(job.get("title", "")).split(" - ")
        company = parts[-1].strip() if len(parts) > 1 else job.get("source", "Unknown Company")
    return {
        "company": company,
        "job_title": job.get("title") or job.get("job_title") or "Unknown",
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "source": job.get("source", ""),
        "id": job.get("id"),
        "source_job_id": job.get("source_job_id"),
        "job_id": job.get("job_id"),
        "title": job.get("title") or job.get("job_title") or "Unknown",
    }


def _queue_record(job, job_key):
    for record in load_queue():
        if (job_key and get_job_key(record) == job_key) or (
            not job_key and record.get("id") == job.get("id")
        ):
            return record
    return None


def _safe_telegram_call(sender, *args, **kwargs):
    """Keep all Telegram failures outside the worker's application failure path."""
    try:
        result = sender(*args, **kwargs)
        return result if isinstance(result, dict) else {"success": False, "error": "TELEGRAM_INVALID_RESULT"}
    except Exception:
        # Do not print exception text: URL/request errors could contain the bot token.
        return {"success": False, "error": "TELEGRAM_UNEXPECTED_ERROR"}


def _persist_notification(job, job_key, tracker_job, field, state):
    current = _queue_record(job, job_key)
    status = current.get("status", "Processing") if current else "Processing"
    mark_job_status(
        job.get("id"), status, job_key=job_key, metadata={field: state},
    )
    update_application_status(tracker_job, status, metadata={field: state})


def _notify_once(job, job_key, tracker_job, event, sender, *args, **kwargs):
    """Persist an event-level attempt before sending so restarts do not spam."""
    field = f"telegram_{event}_notification"
    current = _queue_record(job, job_key)
    if current and current.get(field):
        return current[field]
    attempted = {"attempted_at": utc_now()}
    _persist_notification(job, job_key, tracker_job, field, attempted)
    result = _safe_telegram_call(sender, *args, **kwargs)
    state = {
        **attempted,
        "sent_at": utc_now() if result.get("success") else None,
        "message_id": result.get("message_id"),
        "error": result.get("error"),
    }
    _persist_notification(job, job_key, tracker_job, field, state)
    return state


def _record_failure(job, job_key, reason, detail=None, error_type=None):
    retry_count = int(job.get("retry_count", 0) or 0) + 1
    status = "Failed" if retry_count >= MAX_RETRIES else "Retry"
    failure_reason = "MAX_RETRIES_REACHED" if status == "Failed" else reason
    safe_detail = sanitize_error_text(detail, max_length=240) if detail else None
    last_error = f"{reason}: {safe_detail}" if safe_detail else reason
    last_error = sanitize_error_text(last_error, max_length=240)
    clean_current_url = strip_url_query_and_fragment(job.get("current_url") or job.get("url", ""))
    clean_orig_url = strip_url_query_and_fragment(job.get("url", ""))
    clean_app_url = strip_url_query_and_fragment(job.get("application_url", ""))
    mark_job_status(
        job.get("id"), status, reason=failure_reason, last_error=last_error,
        retry_count=retry_count, job_key=job_key,
        metadata={"original_url": clean_orig_url,
                  "application_url": clean_app_url,
                  "last_failure": {
            "error_type": error_type or reason,
            "error_message": last_error,
            "url": clean_current_url,
            "queue_id": job.get("id"), "job_key": job_key,
            "timestamp": utc_now(),
        }},
    )
    tracker_job = _tracker_job(job)
    save_application(
        tracker_job["company"], tracker_job["job_title"], tracker_job["location"],
        clean_orig_url, tracker_job["source"], status=status,
        job_key=job_key, queue_id=job.get("id"), job=tracker_job,
        failure_reason=failure_reason, retry_count=retry_count,
    )
    update_application_status(
        tracker_job, status, failure_reason=failure_reason, retry_count=retry_count,
        metadata={"last_failure": {
            "error_type": error_type or reason, "error_message": last_error,
            "url": clean_current_url,
            "queue_id": job.get("id"), "job_key": job_key,
            "timestamp": utc_now(),
        }},
    )
    if status == "Failed":
        _notify_once(
            job, job_key, tracker_job, "failed",
            telegram.send_application_result_notification,
            tracker_job, "Failed", failure_reason,
        )
    print(f"[ERROR] Application failed: {failure_reason}; {last_error}")


def _record_navigation_stop(job, job_key, reason):
    """Persist a terminal authorization stop without retrying an unsafe URL."""
    tracker_job = _tracker_job(job)
    mark_job_status(
        job.get("id"), "Failed", reason=reason, job_key=job_key,
        metadata={"original_url": job.get("url", ""),
                  "application_url": job.get("application_url"),
                  "current_url": job.get("current_url") or job.get("url", "")},
    )
    save_application(
        tracker_job["company"], tracker_job["job_title"], tracker_job["location"],
        tracker_job["url"], tracker_job["source"], status="Failed",
        job_key=job_key, queue_id=job.get("id"), job=tracker_job,
        failure_reason=reason, retry_count=int(job.get("retry_count", 0) or 0),
    )
    update_application_status(
        tracker_job, "Failed", failure_reason=reason,
        metadata={"navigation_blocked": True,
                  "original_url": job.get("url", ""),
                  "application_url": job.get("application_url")},
    )
    print(f"[NAVIGATION] Stopped safely: {reason}")
    return {"status": "Failed", "failure_reason": reason}


def _make_wait_callbacks(job, job_key, tracker_job):
    def on_wait(status, reason, details, current_url):
        missing = details.get("missing_fields", []) if isinstance(details, dict) else []
        current = _queue_record(job, job_key)
        same_event = bool(
            current and current.get("waiting_event_id")
            and current.get("waiting_reason") == reason
        )
        detected_at = (current or {}).get("detected_at") if same_event else None
        detected_at = detected_at or utc_now()
        event_id = current.get("waiting_event_id") if same_event else str(uuid.uuid4())
        attempted_at = current.get("human_notification_attempted_at") if same_event else None
        metadata = {
            "waiting_reason": reason,
            "detected_at": detected_at,
            "current_url": current_url,
            "queue_id": job.get("id"),
            "job_key": job_key,
            "waiting_event_id": event_id,
            "missing_fields": missing,
            "last_wait_event": {"status": status, "reason": reason, "detected_at": detected_at},
        }
        if attempted_at:
            metadata["human_notification_attempted_at"] = attempted_at
        mark_job_status(job.get("id"), status, job_key=job_key, metadata=metadata)
        update_application_status(tracker_job, status, metadata=metadata)
        if not attempted_at:
            attempted_at = utc_now()
            _persist_notification(job, job_key, tracker_job,
                                  "human_notification_attempted_at", attempted_at)
            result = _safe_telegram_call(
                telegram.send_human_intervention_notification,
                {**tracker_job, **job, "current_url": current_url}, reason,
                status=status, event_id=event_id,
            )
            notification_metadata = {
                "human_notification_sent_at": utc_now() if result.get("success") else None,
                "human_notification_id": result.get("message_id"),
                "human_notification_error": result.get("error"),
            }
            for key, value in notification_metadata.items():
                if value is None:
                    notification_metadata[key] = None
            mark_job_status(job.get("id"), status, job_key=job_key,
                            metadata=notification_metadata)
            update_application_status(tracker_job, status, metadata=notification_metadata)
        print(f"[HUMAN] Paused: {reason}; queue status {status}; URL: {current_url}")

    def on_resume(current_url):
        resumed_at = utc_now()
        current = _queue_record(job, job_key)
        last_event = dict((current or {}).get("last_wait_event") or {})
        last_event.update({
            "resumed_at": resumed_at,
            "waiting_event_id": (current or {}).get("waiting_event_id"),
            "human_notification_sent_at": (current or {}).get("human_notification_sent_at"),
            "human_notification_id": (current or {}).get("human_notification_id"),
        })
        metadata = {"current_url": current_url, "resumed_at": resumed_at,
                    "last_wait_event": last_event}
        metadata.update({key: None for key in HUMAN_EVENT_FIELDS})
        metadata["waiting_reason"] = None
        mark_job_status(job.get("id"), "Processing", job_key=job_key, metadata=metadata)
        update_application_status(tracker_job, "Processing", metadata=metadata)
        print(f"[HUMAN] Step completed; resuming at {current_url}")

    return on_wait, on_resume


def _wait_for_manual_result(page, on_wait, on_resume):
    deadline = time.monotonic() + MANUAL_RESULT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if page.is_closed():
            return {"status": "NeedsUserInput", "signal": "browser_closed_before_result"}
        gate = detect_verification(page)
        if gate:
            on_wait("WaitingForHuman", gate, {}, str(page.url))
            while not page.is_closed() and detect_verification(page):
                page.wait_for_timeout(1000)
            if page.is_closed():
                return {"status": "WaitingForHuman", "signal": "browser_closed_during_verification"}
            on_resume(str(page.url))
        result = detect_application_result(page)
        if result:
            return result
        page.wait_for_timeout(500)
    return {"status": "Failed", "signal": "TIMEOUT"}


def process_job(page, job, *, applicant=None, auto_submit=None, resume=False,
                navigation_policy=None):
    label = _job_label(job)
    job_key = job.get("job_key") or get_job_key(job)
    print(f"[QUEUE] Found job: {label}")
    print(f"[QUEUE] Job key: {job_key or '(missing)'}")
    score = job.get("suitability_score")
    if score is not None:
        print(f"[QUEUE] Suitability score: {score} ({job.get('suitability_reason', '')})")

    valid, reason = validate_job(job)
    if not valid:
        mark_job_status(job.get("id"), "Failed", reason=reason, job_key=job_key)
        print(f"[ERROR] Reason: {reason}")
        return {"status": "Failed", "failure_reason": reason}

    try:
        navigation_policy = navigation_policy or configured_policy()
        target_url = (job.get("current_url") if resume else None) or job["url"]
        navigation_policy.check(target_url)
        install_page_guard(page, navigation_policy)
    except NavigationRejected as exc:
        return _record_navigation_stop(job, job_key, exc.reason)
    except (KeyError, ValueError):
        return _record_navigation_stop(job, job_key, "NAVIGATION_POLICY_INVALID")
    except Exception:
        return _record_navigation_stop(job, job_key, "NAVIGATION_GUARD_UNAVAILABLE")

    if not resume:
        queued_duplicate = find_protected_queue_duplicate(job, load_queue())
        if queued_duplicate:
            existing_status = queued_duplicate.get("status", "Unknown")
            reason = "ALREADY_APPLIED" if existing_status == "Submitted" else "DUPLICATE_JOB"
            mark_job_status(job.get("id"), "Skipped", reason=reason, job_key=job_key)
            return {"status": "Skipped", "failure_reason": reason}
        duplicate, existing = is_duplicate_application(job)
        if duplicate:
            current = _queue_record(job, job_key)
            protected_statuses = TERMINAL_STATUSES | ACTIVE_STATUSES | WAITING_STATUSES
            if not current or current.get("status") not in protected_statuses:
                mark_job_status(job.get("id"), "Skipped", reason="ALREADY_APPLIED", job_key=job_key)
            print(
                f"[QUEUE] Duplicate detected; existing status: "
                f"{(existing or current or {}).get('status', 'Unknown')}"
            )
            print("[QUEUE] Skipping duplicate application attempt")
            return {"status": "Skipped", "failure_reason": "ALREADY_APPLIED", "existing_status": existing.get("status")}

    tracker_job = _tracker_job(job)
    if applicant is None:
        applicant = load_applicant_profile()
    submit_enabled = resolve_auto_submit(auto_submit)

    if resume:
        mark_job_status(job.get("id"), "Processing", job_key=job_key,
                        metadata={"resumed_at": utc_now()})
        update_application_status(tracker_job, "Processing", metadata={"resumed_at": utc_now()})
    else:
        save_application(
            tracker_job["company"], tracker_job["job_title"], tracker_job["location"],
            tracker_job["url"], tracker_job["source"], status="Processing",
            job_key=job_key, queue_id=job.get("id"), job=tracker_job,
            retry_count=int(job.get("retry_count", 0) or 0),
        )
        mark_job_status(job.get("id"), "Started", job_key=job_key)
        mark_job_status(job.get("id"), "Processing", job_key=job_key)

    on_wait, on_resume = _make_wait_callbacks(job, job_key, tracker_job)
    try:
        navigation_policy.clear_rejection()
        if str(getattr(page, "url", "")) != target_url:
            page.goto(target_url, wait_until="load", timeout=60000)
        current_page_is_authorized(page, navigation_policy)
    except Exception as exc:
        if navigation_policy.last_rejection:
            return _record_navigation_stop(job, job_key, navigation_policy.last_rejection)
        safe_exc = sanitize_error_text(exc, max_length=240)
        _record_failure(job, job_key, "NAVIGATION_FAILED", safe_exc, type(exc).__name__)
        return {"status": "Retry", "failure_reason": "NAVIGATION_FAILED"}

    print("[QUEUE] Duplicate check: PASS")
    print("[WORKER] Analyzing and preparing application form")
    result = apply_to_job(
        page, tracker_job, applicant, auto_submit=submit_enabled,
        job_key=job_key, queue_id=job.get("id"),
        on_wait=on_wait, on_resume=on_resume,
        navigation_policy=navigation_policy,
    )
    status = result.get("status")
    job["application_url"] = result.get("application_url") or job.get("application_url")
    if status in {"WaitingForHuman", "NeedsUserInput"}:
        # The page remains open and the persistent queue state is resumable at next startup.
        current = _queue_record(job, job_key)
        mark_job_status(job.get("id"), (current or {}).get("status", status),
                        job_key=job_key,
                        metadata={"original_url": job.get("url", ""),
                                  "application_url": job.get("application_url")})
        return result
    if status == "Failed":
        reason = result.get("failure_reason", "FORM_PREPARATION_FAILED")
        if reason in {"UNAUTHORIZED_HOST", "UNSUPPORTED_NAVIGATION_URL", "NAVIGATION_POLICY_INVALID"}:
            _record_navigation_stop(job, job_key, reason)
        else:
            _record_failure(job, job_key, reason,
                            result.get("reason"), result.get("error", {}).get("error_type"))
        return result
    if status == "Submitted":
        mark_job_status(job.get("id"), "Submitted", job_key=job_key,
                        metadata={"original_url": job.get("url", ""),
                                  "application_url": job.get("application_url")})
        update_application_status(tracker_job, "Submitted")
        _notify_once(job, job_key, tracker_job, "submitted",
                     telegram.send_application_result_notification,
                     tracker_job, "Submitted")
        return result
    if status != "ReadyForSubmission":
        return result

    mark_job_status(job.get("id"), "ReadyForSubmission", job_key=job_key,
                    metadata={"form_validated": True, "missing_fields": [],
                              "original_url": job.get("url", ""),
                              "application_url": job.get("application_url")})
    if not submit_enabled:
        _notify_once(job, job_key, tracker_job, "ready",
                     telegram.send_application_ready_notification, tracker_job)
    print("[WORKER] Form validated; submission remains manual unless AUTO_SUBMIT is explicitly enabled.")
    outcome = _wait_for_manual_result(page, on_wait, on_resume)
    if outcome["status"] == "Submitted":
        mark_job_status(job.get("id"), "Submitted", job_key=job_key,
                        metadata={"original_url": job.get("url", ""),
                                  "application_url": job.get("application_url")})
        update_application_status(tracker_job, "Submitted")
        _notify_once(job, job_key, tracker_job, "submitted",
                     telegram.send_application_result_notification,
                     tracker_job, "Submitted")
    elif outcome["status"] == "Failed":
        _record_failure(job, job_key, outcome.get("signal", "TIMEOUT"), error_type="SubmissionTimeout")
    else:
        mark_job_status(job.get("id"), outcome["status"], job_key=job_key,
                        metadata={"waiting_reason": outcome.get("signal"), "current_url": str(getattr(page, "url", ""))})
        update_application_status(tracker_job, outcome["status"], metadata={"waiting_reason": outcome.get("signal")})
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local job application worker.")
    parser.parse_args([] if argv is None else argv)
    print("VIVEK JOB AUTOMATION — Phase 2 worker")
    recovered = recover_stale_jobs(stale_after_seconds=RECOVERY_STALE_SECONDS)
    for job in recovered:
        reference = _tracker_job(job)
        update_application_status(reference, job["status"], failure_reason=job.get("failure_reason"), retry_count=job.get("retry_count"))

    navigation_policy = configured_policy()
    with sync_playwright() as playwright:
        _, context, page = start_browser(playwright, navigation_policy=navigation_policy)
        print(f"[WORKER] Browser ready; AUTO_SUBMIT={auto_submit_enabled()}")
        waiting = get_waiting_job()
        if waiting:
            print(f"[WORKER] Resuming waiting job: {_job_label(waiting)}")
            process_job(page, waiting, resume=True, navigation_policy=navigation_policy)
        while True:
            job = get_next_job()
            if not job:
                time.sleep(30)
                continue
            process_job(page, job, navigation_policy=navigation_policy)
            time.sleep(3)


if __name__ == "__main__":
    main(sys.argv[1:])
