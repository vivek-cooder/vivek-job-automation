"""Read-only alert-repository sync plus local suitability scoring."""

import json
import re
import subprocess
from pathlib import Path

from worker.job_queue import (
    BASE_DIR, TERMINAL_STATUSES, WAITING_STATUSES, add_job,
    find_duplicate, load_queue, write_queue,
)
from worker.tracker import is_duplicate_application
from worker.sanitizer import sanitize_error_text

GITHUB_REPO = Path(r"D:\project\JOB\vivek-job-alert")
REMOTE_QUEUE = GITHUB_REPO / "data" / "application_queue.json"
LOCAL_QUEUE = BASE_DIR / "data" / "application_queue.json"
TRACKER_FILE = BASE_DIR / "data" / "applications.json"

TARGET_ROLES = (
    # Frontend & React Development
    "react developer", "react engineer", "react.js developer",
    "frontend developer", "frontend engineer", "front-end developer", "front-end engineer",
    "web developer", "junior web developer", "javascript developer",

    # UI/UX Design (Equal Priority)
    "ui/ux designer", "ui ux designer", "ui designer", "ux designer",
    "product designer", "ui/ux specialist",

    # Hybrid UI/UX & Frontend Development
    "ui/ux developer", "ui ux developer", "ui developer", "ux developer",
    "ui engineer", "design technologist",
)
HIGH_PRIORITY = TARGET_ROLES

RELATED_ROLES = (
    "junior software developer", "junior software engineer", "junior developer",
    "software developer", "software engineer", "associate software engineer", "associate developer",
    "graduate engineer", "frontend", "react", "ui/ux", "ui ux", "web development",
    "web developer", "javascript",
)
RELATED = RELATED_ROLES

UNRELATED = (
    "sales", "marketing", "human resources", " hr ", "accountant",
    "mechanical engineer", "civil engineer", "driver", "data entry",
    "telecaller",
)

EXCLUDED_SUPPORT_ROLES = (
    "it support", "helpdesk", "help desk", "desktop support",
    "application support", "technical support", "tech support",
    "service desk", "it technician", "systems administrator", "system admin",
    "network engineer", "hardware technician",
)

LOCATIONS = ("mumbai", "navi mumbai", "thane", "dombivli", "kalyan")
ENTRY_LEVEL = (
    "fresher", "entry level", "entry-level", "intern", "internship",
    "trainee", "junior", "0 years", "0-1 years", "0–1 years", "0 to 1 years",
    "0-6 months",
)

SKILLS = (
    "javascript", "react.js", "react", "typescript", "html", "css",
    "figma", "ui/ux", "ui ux", "tailwind", "next.js", "redux", "web developer",
)

SENIOR_TITLE_KEYWORDS = (
    "senior", "sr.", "sr ", "lead", "principal", "staff",
    "architect", "director", "manager", "head of",
)


def _text(*values):
    return " ".join(str(value or "") for value in values).casefold()


def _normalize_title(value):
    title = str(value or "").casefold()
    title = re.sub(r"\bui\s*[/\-–—]\s*ux\b", "ui ux", title)
    return re.sub(r"\s+", " ", title).strip()


def _parse_experience_years(value_str):
    val = float(value_str)
    return int(val) if val.is_integer() else val


def extract_min_experience_years(text):
    """Extract minimum required experience in years from text; returns None if unstated."""
    if not text:
        return None
    # 1. Experience Ranges: "2-5 years", "3 to 5 yrs", "0–2 years", "1.5–3 years" -> extract lower bound (group 1)
    range_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:to|-|–|—)\s*\d+(?:\.\d+)?\s*(?:years?|yrs?)\b(?!\s+in\s+business)", text, re.IGNORECASE)
    if range_match:
        return _parse_experience_years(range_match.group(1))

    # 2. Explicit Minimums: "at least 3 years", "minimum 4 yrs", "min 5 years", "at least 2.5 years" -> extract group 1
    min_match = re.search(r"\b(?:at\s+least|minimum|min\.?)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b(?!\s+in\s+business)", text, re.IGNORECASE)
    if min_match:
        return _parse_experience_years(min_match.group(1))

    # 3. Plus Formats: "3+ years", "5+ yrs", "7+ years", "2.5+ years" -> extract group 1 (ignoring company tenure "in business")
    plus_match = re.search(r"\b(\d+(?:\.\d+)?)\+\s*(?:years?|yrs?)\b(?!\s+in\s+business)", text, re.IGNORECASE)
    if plus_match:
        return _parse_experience_years(plus_match.group(1))

    # 4. Direct Requirements: "3 years of relevant experience", "3 years of experience", "4 years exp", "2.5 years of experience" -> extract group 1
    direct_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:years?|yrs?)(?:\s+of)?(?:\s+relevant)?\s+(?:experience|exp)\b(?!\s+in\s+business)", text, re.IGNORECASE)
    if direct_match:
        return _parse_experience_years(direct_match.group(1))

    return None


def score_job(job):
    title = _normalize_title(job.get("title") or job.get("job_title"))
    description = _text(job.get("snippet"), job.get("description"), job.get("experience"), job.get("seniority"))
    location = _text(job.get("location"), description)
    score = 0
    reasons = []

    # Title classification (Mutually exclusive: at most one title tier bonus)
    if any(term in title for term in TARGET_ROLES):
        score += 5
        reasons.append("target role +5")
    elif any(term in title for term in RELATED_ROLES):
        score += 3
        reasons.append("related role +3")

    # Skills recognition
    if any(term in description for term in SKILLS):
        score += 3
        reasons.append("tech/design skills +3")

    # Location preferences
    if "remote" in location:
        score += 3
        reasons.append("remote +3")
    elif any(term in location for term in LOCATIONS):
        score += 2
        reasons.append("preferred location +2")

    # Entry-level / experience bonuses
    if any(term in description for term in ENTRY_LEVEL):
        score += 3
        reasons.append("entry level +3")
    if "0-2 years" in description or "0–2 years" in description or "0 to 2 years" in description or "0 - 2 years" in description:
        score += 2
        reasons.append("0–2 years +2")

    # Excluded support role penalty
    if any(term in title for term in EXCLUDED_SUPPORT_ROLES):
        score -= 10
        reasons.append("excluded support role -10")

    # Unrelated role penalty
    if any(term in f" {title} " for term in UNRELATED):
        score -= 10
        reasons.append("clearly unrelated role -10")

    # Seniority penalty
    is_senior_title = any(term in title for term in SENIOR_TITLE_KEYWORDS)
    if is_senior_title:
        min_exp = extract_min_experience_years(description)
        if min_exp is not None and min_exp >= 3:
            score -= 7
            reasons.append("senior experience requirement -7")
        else:
            score -= 2
            reasons.append("senior title keyword -2")

    return score, "; ".join(reasons) or "no strong profile signal; retained for review"


def evaluate_job(job):
    score, reason = score_job(job)
    title = _normalize_title(job.get("title") or job.get("job_title"))
    clearly_unrelated = any(term in f" {title} " for term in UNRELATED)
    is_support_role = any(term in title for term in EXCLUDED_SUPPORT_ROLES)
    result = dict(job)
    result["suitability_score"] = score
    result["suitability_reason"] = reason

    if clearly_unrelated:
        result["status"] = "Skipped"
        result["skip_reason"] = "UNRELATED_ROLE"
    elif is_support_role:
        result["status"] = "Skipped"
        result["skip_reason"] = "EXCLUDED_SUPPORT_ROLE"

    return result


def load_json(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def sync_jobs(remote_jobs, *, local_path=LOCAL_QUEUE, tracker_path=TRACKER_FILE):
    jobs = load_queue(local_path)
    added = updated = skipped = duplicates = 0
    for incoming in remote_jobs:
        evaluated = evaluate_job(incoming)
        duplicate = find_duplicate(evaluated, jobs)
        if duplicate:
            # Refresh descriptive fields and scoring only. Never regress local state.
            state_fields = {"id", "job_key", "source_job_id", "job_id", "status", "created_at",
                            "started_at", "completed_at", "updated_at", "retry_count",
                            "failure_reason", "last_error", "last_interrupted_status"}
            for key, value in evaluated.items():
                if key not in state_fields and value not in (None, ""):
                    duplicate[key] = value
            updated += 1
            duplicates += 1
            continue
        is_valid = bool(evaluated.get("title") or evaluated.get("job_title")) and bool(evaluated.get("url"))
        if not is_valid:
            print("[SYNC] Ignored invalid job: title and URL are required")
            skipped += 1
            continue
        existing_application, _ = is_duplicate_application(evaluated, path=tracker_path)
        if existing_application:
            evaluated["status"] = "Skipped"
            evaluated["skip_reason"] = "ALREADY_APPLIED"
        elif evaluated.get("status") != "Skipped":
            evaluated["status"] = "Queued"
        did_add, record, reason = add_job(evaluated, local_path)
        if did_add:
            jobs.append(record)
            if record.get("status") == "Skipped":
                skipped += 1
            else:
                added += 1
        elif reason == "DUPLICATE_JOB":
            duplicates += 1
    if updated:
        write_queue(jobs, local_path)
    return {"added": added, "updated": updated, "duplicates": duplicates, "skipped": skipped}


def sync_queue():
    print("[SYNC] Reading latest alert jobs (alert repository is read-only here)...")
    result = subprocess.run(
        ["git", "-C", str(GITHUB_REPO), "pull", "--ff-only"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        error_summary = sanitize_error_text(result.stderr.strip(), max_length=120)
        print(f"[SYNC] Alert repository pull failed: {error_summary or 'GIT_PULL_ERROR'}")
        return
    summary = sync_jobs(load_json(REMOTE_QUEUE))
    print("[SYNC] Added {added}; updated {updated}; duplicates {duplicates}; skipped {skipped}".format(**summary))


if __name__ == "__main__":
    sync_queue()
