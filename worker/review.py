"""Phase 14 Controlled Preparation & Read-Only Inspection Mode.

Inspects job listings and application forms without filling inputs,
clicking submission controls, or logging personal values.
"""

import re
from urllib.parse import urlsplit, urljoin

from worker.detector import detect_verification
from worker.form_adapter import (
    identify_application_form,
    identify_application_link,
    _form_fields,
    APPLICANT_KEYS,
)
from worker.navigation import NavigationPolicy, current_page_is_authorized

PROFILE_KEY_LABELS = {
    "name": "Full Name",
    "email": "Email Address",
    "phone": "Phone Number",
    "location": "Location / City",
    "position": "Target Position / Title",
    "portfolio": "Portfolio / Personal Website URL",
    "linkedin": "LinkedIn Profile URL",
    "github": "GitHub Profile URL",
    "experience": "Experience Summary",
    "skills": "Skills / Technologies",
    "resume_path": "Resume File Path",
    "cover_letter": "Cover Letter Text",
    "work_authorization": "Work Authorization Status",
    "sponsorship": "Visa Sponsorship Need",
    "notice_period": "Notice Period / Availability",
    "salary": "Salary Expectations",
    "education": "Education Background",
    "relocation": "Willingness to Relocate",
}


def inspect_listing_metadata(page, job=None):
    """Extract job title, company, URL, and route without side effects.

    Never modifies the DOM, clicks buttons, or writes to queue/tracker.
    """
    job = job or {}
    url = str(getattr(page, "url", job.get("url", "")))
    title = job.get("title") or job.get("job_title")
    company = job.get("company")

    try:
        page_title = page.title()
    except Exception:
        page_title = None

    # Heading inspection
    h1_text = None
    try:
        h1 = page.locator("h1").first
        if h1.count() > 0 and h1.is_visible():
            h1_text = h1.inner_text(timeout=1000).strip()
    except Exception:
        h1_text = None

    detected_title = title or h1_text or page_title
    detected_company = company or "Unknown Company"

    # Application route inspection (link vs embedded form)
    link_result = identify_application_link(page)
    form_result = identify_application_form(page)

    route_type = "none"
    application_route = None
    if form_result.get("status") == "Selected":
        route_type = "embedded_form"
        application_route = url
    elif link_result.get("status") == "Selected":
        route_type = "link_or_anchor"
        application_route = link_result.get("url")

    return {
        "url": url,
        "page_title": page_title,
        "detected_title": detected_title,
        "detected_company": detected_company,
        "route_type": route_type,
        "application_route": application_route,
        "application_link_status": link_result.get("status"),
        "application_form_status": form_result.get("status"),
    }


def review_application_form(page, profile_keys=None):
    """Inspect and map form fields without filling or transmitting data.

    Returns mapping of fields to profile keys (keys only, no values).
    Identifies missing required fields, ambiguous forms, and verification gates.
    """
    verification = detect_verification(page)
    if verification:
        return {
            "status": "WaitingForHuman",
            "reason": verification,
            "fields": [],
            "required_fields": [],
            "missing_required_keys": [],
            "unknown_required_fields": [],
            "can_proceed_to_fill_after_approval": False,
            "submission_prevented": True,
        }

    selection = identify_application_form(page)
    status = selection.get("status")
    if status != "Selected":
        return {
            "status": status,
            "reason": selection.get("reason", "NO_APPLICATION_FORM_SELECTED"),
            "candidate_count": selection.get("candidate_count", 0),
            "fields": [],
            "required_fields": [],
            "missing_required_keys": [],
            "unknown_required_fields": [],
            "can_proceed_to_fill_after_approval": False,
            "submission_prevented": True,
        }

    form = selection["form"]
    fields = _form_fields(form)

    field_reviews = []
    required_fields = []
    unknown_required = []
    mapped_required_keys = set()

    for f in fields:
        field_type = f.get("field_type", "unknown")
        profile_key = APPLICANT_KEYS.get(field_type)
        is_required = bool(f.get("required"))
        name_or_id = f.get("name") or f.get("id") or f.get("label") or f"field_{f.get('index')}"

        info = {
            "index": f.get("index"),
            "control_type": f.get("type"),
            "name": f.get("name"),
            "id": f.get("id"),
            "label": f.get("label"),
            "required": is_required,
            "field_type": field_type,
            "confidence": f.get("confidence", 0.0),
            "mapped_profile_key": profile_key,
            "profile_key_label": PROFILE_KEY_LABELS.get(profile_key) if profile_key else None,
        }
        field_reviews.append(info)

        if is_required:
            required_fields.append(info)
            if field_type == "unknown" or not profile_key:
                unknown_required.append(name_or_id)
            else:
                mapped_required_keys.add(profile_key)

    # Check which required profile keys are missing in the provided profile
    missing_required_keys = []
    if profile_keys is not None:
        if isinstance(profile_keys, dict):
            # Check for missing or empty keys without logging values
            for key in mapped_required_keys:
                val = profile_keys.get(key)
                if val is None or not str(val).strip():
                    missing_required_keys.append(key)
        elif isinstance(profile_keys, (set, list, tuple)):
            available = set(profile_keys)
            for key in mapped_required_keys:
                if key not in available:
                    missing_required_keys.append(key)

    missing_required_keys.sort()
    unknown_required.sort()

    can_proceed = (
        not verification
        and not unknown_required
        and not missing_required_keys
        and len(field_reviews) > 0
    )

    return {
        "status": "ReadyForHumanReview" if can_proceed else "NeedsUserInput",
        "form_score": selection.get("score"),
        "field_count": len(field_reviews),
        "fields": field_reviews,
        "required_fields_count": len(required_fields),
        "mapped_required_keys": sorted(mapped_required_keys),
        "missing_required_keys": missing_required_keys,
        "unknown_required_fields": unknown_required,
        "verification_detected": verification,
        "can_proceed_to_fill_after_approval": can_proceed,
        "submission_prevented": True,
    }


def prepare_job_review(page, target_url, *, job=None, profile_keys=None, navigation_policy=None):
    """Execute a fully read-only preparation review of a job listing.

    Guarantees:
    - Never fills form controls.
    - Never clicks submission buttons.
    - Never transmits personal profile values.
    - Respects navigation policy.
    - Returns structured readiness report for human sign-off.
    """
    if navigation_policy:
        navigation_policy.check(target_url)
        current_page_is_authorized(page, navigation_policy)

    listing_meta = inspect_listing_metadata(page, job=job)
    form_review = review_application_form(page, profile_keys=profile_keys)

    return {
        "url": target_url,
        "listing": listing_meta,
        "form_review": form_review,
        "submission_action_available": False,
        "auto_submit_enforced": False,
    }
