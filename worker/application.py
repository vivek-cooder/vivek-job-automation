from worker.form_adapter import prepare_application
from worker.ai import create_browser_agent
from worker.settings import auto_submit_enabled, resolve_auto_submit
from worker.tracker import save_application, update_application_status
from worker.sanitizer import sanitize_error_text, strip_url_query_and_fragment


def apply_to_job(page, job, applicant, auto_submit=None, *, job_key=None,
                 queue_id=None, on_wait=None, on_resume=None, navigation_policy=None):
    """Analyze/fill a form, safely pause for people, and gate submission by config."""
    company = job.get("company", "Unknown Company")
    job_title = job.get("job_title") or job.get("title", "Unknown Position")
    location = job.get("location", "")
    url = job.get("url", "")
    source = job.get("source", "")
    tracker_job = dict(job, title=job_title, id=queue_id)
    submit_enabled = resolve_auto_submit(auto_submit)
    print(f"[WORKER] Preparing application: {job_title} - {company}")
    try:
        ai_agent = create_browser_agent(queue_id)
    except Exception:
        # AI is optional; a faulty opt-in setup must not compromise the
        # deterministic worker or turn into an application failure.
        ai_agent = None
    save_application(
        company, job_title, location, url, source, status="Processing",
        job_key=job_key, queue_id=queue_id, job=tracker_job,
    )
    try:
        result = prepare_application(
            page, applicant, auto_submit=submit_enabled,
            on_wait=on_wait, on_resume=on_resume,
            ai_agent=ai_agent,
            navigation_policy=navigation_policy,
        )
        status = result.get("status", "Failed")
        reason = result.get("failure_reason") or result.get("reason")
        metadata = {
            "form_results": result.get("fields", []),
            "ai_actions": result.get("ai_actions", []),
            "missing_fields": result.get("missing_fields", []),
            "form_validated": result.get("valid_for_submission", False),
            "current_url": str(getattr(page, "url", url)),
            "original_url": result.get("original_url", url),
            "application_url": result.get("application_url", str(getattr(page, "url", url))),
            "submission_enabled": result.get("submission_enabled", submit_enabled),
            "submission_blocked_reason": result.get("submission_blocked_reason"),
        }
        update_application_status(
            tracker_job, status,
            failure_reason=reason if status == "Failed" else None,
            metadata=metadata,
        )
        if status == "ReadyForSubmission":
            print("[WORKER] Form validated; ready for manual submission.")
        elif status == "Submitted":
            print("[TRACKER] Submission confirmed by page signal.")
        elif status in {"WaitingForHuman", "NeedsUserInput"}:
            print(f"[WORKER] Paused with status {status}; browser remains open.")
        elif status == "Failed":
            print(f"[ERROR] {result.get('failure_reason', 'FORM_PREPARATION_FAILED')}")
        return result
    except Exception as exc:
        sanitized_message = sanitize_error_text(exc, max_length=240)
        clean_url = strip_url_query_and_fragment(str(getattr(page, "url", url)))
        error = {
            "error_type": type(exc).__name__,
            "error_message": sanitized_message,
            "url": clean_url,
            "queue_id": queue_id,
            "job_key": job_key,
        }
        print(f"[ERROR] Form preparation failed: {sanitized_message}")
        update_application_status(
            tracker_job, "Failed", failure_reason="FORM_PREPARATION_FAILED",
            metadata={"last_failure": error},
        )
        return {
            "status": "Failed", "failure_reason": "FORM_PREPARATION_FAILED",
            "error": error, "fields": [], "missing_fields": [],
        }
