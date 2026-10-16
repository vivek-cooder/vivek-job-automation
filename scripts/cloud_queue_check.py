```python
"""Read-only cloud check of the job-alert queue.

Features:
- Existing suitability scoring
- Duplicate detection
- 3+ years experience review flags
- Conservative job-aggregator page detection
- India-eligibility/location prioritization
- Hybrid UI/UX + frontend role detection
- Paid, unpaid, and pay-not-stated indicators

This script never writes to the source queue or local tracker.
It does not launch a browser or submit applications.
"""

import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from worker.sync_queue import evaluate_job, extract_min_experience_years


API_URL = (
    "https://api.github.com/repos/vivek-cooder/vivek-job-alert/"
    "contents/data/application_queue.json?ref=main"
)


def safe_log(value, limit=220):
    """Sanitize log text, including GitHub Actions workflow commands."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit]
    return " " + text if text.startswith("::") else text


def plain_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def job_text(job, *keys):
    return " ".join(plain_text(job.get(key)) for key in keys).strip()


def fetch_queue(token):
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vivek-job-automation-readonly-check",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict) or not isinstance(
        payload.get("content"), str
    ):
        raise ValueError("GitHub API response did not contain file content.")

    raw = base64.b64decode(payload["content"]).decode("utf-8")
    jobs = json.loads(raw)

    if not isinstance(jobs, list):
        raise ValueError("The remote queue must be a JSON list.")

    return jobs


def normalized_title(job):
    title = plain_text(job.get("title") or job.get("job_title"))
    title = re.sub(
        r"\s*[|–—-]\s*(?:LinkedIn Jobs?.*|Indeed.*|Naukri\.com.*)$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", title).strip().casefold()


def dedupe_key(job):
    title = normalized_title(job)
    location = plain_text(job.get("location")).casefold()
    snippet = str(job.get("snippet") or "")

    match = re.search(
        r"""href=["'](https?://[^"']+)["']""",
        snippet,
        flags=re.IGNORECASE,
    )
    candidate_url = match.group(1) if match else str(job.get("url") or "")
    parsed = urllib.parse.urlsplit(candidate_url)

    if parsed.netloc.casefold() == "news.google.com":
        return ("title-location", title, location)

    canonical_url = urllib.parse.urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )

    if parsed.scheme in ("http", "https") and parsed.netloc:
        return ("url", canonical_url)

    return ("title-location", title, location)


def experience_flag(job):
    """Flag possible 3+ year requirements for manual review, not rejection."""
    title = plain_text(job.get("title") or job.get("job_title"))
    description = job_text(
        job, "snippet", "description", "experience", "seniority"
    )

    title_years = extract_min_experience_years(title)
    description_years = extract_min_experience_years(description)
    years = title_years if title_years is not None else description_years

    if years is not None and years >= 3:
        return years, "REVIEW_EXPERIENCE_3_PLUS_YEARS"

    return years, None


def role_bucket(job):
    """Categorize a role using its title, not unrelated description mentions."""
    title = plain_text(
        job.get("title") or job.get("job_title")
    ).casefold()

    title = re.sub(r"\bui\s*[/\-–—]\s*ux\b", "ui ux", title)
    title = re.sub(r"\bfront[\s-]?end\b", "frontend", title)
    title = re.sub(r"\s+", " ", title).strip()

    has_design = bool(
        re.search(
            r"\b(ui ux|ux ui|ui|ux|user experience|user interface|"
            r"product|visual|interaction)\s+"
            r"(designer|design|designing|specialist)\b",
            title,
        )
        or re.search(r"\b(product designer|visual designer)\b", title)
    )

    has_frontend = bool(
        re.search(
            r"\b(frontend|react|javascript|web developer|"
            r"web engineer|frontend engineer|ui developer|"
            r"ui engineer|design technologist)\b",
            title,
        )
    )

    hybrid_terms = (
        "ui ux developer",
        "ui ux engineer",
        "ux ui developer",
        "ui developer",
        "ux developer",
        "ui engineer",
        "design technologist",
        "frontend designer",
        "ui ux frontend",
        "ui ux front end",
    )

    if any(term in title for term in hybrid_terms):
        return "Hybrid UI/UX + Frontend"

    if has_design and has_frontend:
        return "Hybrid UI/UX + Frontend"

    design_terms = (
        "ui ux designer",
        "ux ui designer",
        "ui designer",
        "ux designer",
        "product designer",
        "ui ux specialist",
        "user experience designer",
        "user interface designer",
        "visual designer",
        "interaction designer",
    )
    if any(term in title for term in design_terms):
        return "UI/UX Design"

    frontend_terms = (
        "react developer",
        "react engineer",
        "react.js developer",
        "react js developer",
        "frontend developer",
        "frontend engineer",
        "web developer",
        "javascript developer",
        "frontend",
    )
    if any(term in title for term in frontend_terms):
        return "Frontend / React"

    return "Other related roles"


def location_classification(job):
    """Classify location conservatively; never assume foreign roles are eligible."""
    location = job_text(job, "location").casefold()
    title = plain_text(job.get("title") or job.get("job_title")).casefold()
    description = job_text(job, "snippet", "description").casefold()

    india_markers = (
        "india",
        "mumbai",
        "navi mumbai",
        "thane",
        "dombivli",
        "kalyan",
        "bengaluru",
        "bangalore",
        "pune",
        "hyderabad",
        "chennai",
        "delhi",
        "noida",
        "gurugram",
        "gurgaon",
        "kolkata",
        "ahmedabad",
        "indore",
        "jaipur",
        "kochi",
    )

    # Prefer explicit location fields over general description mentions.
    if location:
        if any(marker in location for marker in india_markers):
            return "INDIA_OR_INDIA_REMOTE", 0
        if "remote" in location and not any(
            country in location
            for country in (
                "united states",
                "usa",
                "u.s.",
                "united kingdom",
                "uk",
                "ireland",
                "canada",
                "australia",
                "germany",
                "france",
                "singapore",
            )
        ):
            return "REMOTE_LOCATION_UNCLEAR", 1
        if any(
            country in location
            for country in (
                "united states",
                "usa",
                "u.s.",
                "united kingdom",
                "uk",
                "ireland",
                "canada",
                "australia",
                "germany",
                "france",
                "singapore",
            )
        ):
            return "FOREIGN_LOCATION_REVIEW", 2

    # Use explicit India-remote wording in title/description only as a fallback.
    if re.search(r"\b(remote|work from home|wfh)\b.{0,35}\b(india|india only)\b", title):
        return "INDIA_OR_INDIA_REMOTE", 0

    if re.search(r"\b(india|india only)\b.{0,35}\b(remote|work from home|wfh)\b", title):
        return "INDIA_OR_INDIA_REMOTE", 0

    if re.search(r"\b(remote|work from home|wfh)\b.{0,35}\bindia\b", description):
        return "INDIA_OR_INDIA_REMOTE", 0

    return "LOCATION_UNCLEAR_REVIEW", 1


def pay_classification(job):
    """Identify pay signals without inferring pay from an internship title alone."""
    title = plain_text(job.get("title") or job.get("job_title"))
    description = job_text(
        job, "snippet", "description", "salary", "compensation", "pay"
    )
    text = f"{title} {description}".casefold()

    unpaid_patterns = (
        r"\bunpaid\b",
        r"\bwithout stipend\b",
        r"\bno stipend\b",
        r"\bzero stipend\b",
        r"\bnon[- ]paid\b",
        r"\bvolunteer(?:ing)?\b",
    )
    if any(re.search(pattern, text) for pattern in unpaid_patterns):
        return "UNPAID", 2

    paid_patterns = (
        r"\bpaid internship\b",
        r"\bstipend\b",
        r"₹\s*[\d,]+",
        r"\binr\s*[\d,]+",
        r"\brs\.?\s*[\d,]+",
        r"\bsalary\b",
        r"\bcompensation\b",
        r"\bper annum\b",
        r"\bper month\b",
        r"\b\d[\d,.]*\s*(?:lpa|lakhs? per annum)\b",
        r"\$\s*[\d,]+",
    )
    if any(re.search(pattern, text) for pattern in paid_patterns):
        return "PAY_MENTIONED_CHECK_DETAILS", 0

    return "PAY_NOT_STATED", 1


def aggregator_page_flag(job):
    """Conservatively identify likely search-result pages, not real vacancies."""
    title = plain_text(job.get("title") or job.get("job_title"))
    url = str(job.get("url") or "").casefold()
    text = title.casefold()

    # These are strong search-page signals, not generic uses of the word 'jobs'.
    patterns = (
        r"\b\d[\d,]*\+?\s+.*\bjobs?\s+in\b",
        r"\b\d[\d,]*\+?\s+.*\b(open roles|openings)\b",
        r"\bjobs? in [^|–—]{2,60}\(\s*\d[\d,]*\s+open",
        r"\b(remote|work from home)\s+.*\bjobs?\s+in\b",
        r"\b(job search|job listings|job results)\b",
        r"\b\d[\d,]*\+?\s+(?:react|frontend|front end|web developer|"
        r"ui.?ux|javascript).*\bjobs?\b",
    )

    search_path = any(
        marker in url
        for marker in (
            "/jobs/search",
            "/jobs/collections",
            "/jobs/see-more",
            "/jobs/?",
            "/job-search",
            "/job-search/",
            "/search/jobs",
        )
    )

    if search_path or any(re.search(pattern, text) for pattern in patterns):
        return True, "REVIEW_POSSIBLE_AGGREGATOR_PAGE"

    return False, None


def get_score(job):
    try:
        return float(job.get("suitability_score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def sort_key(job):
    """Prefer India-eligible and non-unpaid roles, then existing suitability score."""
    return (
        job.get("location_priority", 1),
        job.get("pay_priority", 1),
        -get_score(job),
        normalized_title(job),
    )


def display_candidates(jobs, limit=10):
    for job in jobs[:limit]:
        title = safe_log(job.get("title") or job.get("job_title"))
        score = job.get("suitability_score", 0)
        location_label = job.get("location_classification", "LOCATION_UNCLEAR_REVIEW")
        pay_label = job.get("pay_classification", "PAY_NOT_STATED")
        reason = safe_log(job.get("suitability_reason", ""))

        print(
            f"- score={score} | location={location_label} | pay={pay_label}"
            f" | {title}"
        )
        if reason:
            print(f"  scoring: {reason}")

    if not jobs:
        print("- No candidates in this category.")


def main():
    token = os.environ.get("JOB_ALERTS_READ_TOKEN", "").strip()

    if not token:
        print("ERROR: Repository secret JOB_ALERTS_READ_TOKEN is missing.")
        return 2

    try:
        remote_jobs = fetch_queue(token)
    except urllib.error.HTTPError as exc:
        print(
            f"ERROR: GitHub returned HTTP {exc.code} "
            "while reading the queue."
        )
        return 1
    except (urllib.error.URLError, TimeoutError):
        print("ERROR: Could not reach GitHub to read the queue.")
        return 1
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(
            f"ERROR: Queue response is invalid ({type(exc).__name__})."
        )
        return 1

    valid = []
    invalid_count = 0

    for job in remote_jobs:
        if not isinstance(job, dict):
            invalid_count += 1
            continue

        title = str(job.get("title") or job.get("job_title") or "").strip()
        url = str(job.get("url") or "").strip()
        parsed_url = urllib.parse.urlsplit(url)

        if (
            not title
            or parsed_url.scheme != "https"
            or not parsed_url.netloc
        ):
            invalid_count += 1
            continue

        evaluated = evaluate_job(job)

        years, flag = experience_flag(evaluated)
        evaluated["review_experience_years"] = years
        evaluated["review_flag"] = flag

        location_label, location_priority = location_classification(evaluated)
        evaluated["location_classification"] = location_label
        evaluated["location_priority"] = location_priority

        pay_label, pay_priority = pay_classification(evaluated)
        evaluated["pay_classification"] = pay_label
        evaluated["pay_priority"] = pay_priority

        aggregator_flag, aggregator_reason = aggregator_page_flag(evaluated)
        evaluated["possible_aggregator_page"] = aggregator_flag
        evaluated["aggregator_review_flag"] = aggregator_reason

        evaluated["role_category"] = role_bucket(evaluated)
        valid.append(evaluated)

    excluded = [
        job for job in valid
        if job.get("status") == "Skipped"
    ]
    candidates = [
        job for job in valid
        if job.get("status") != "Skipped"
    ]

    # Collapse duplicates, retaining the best-scoring record for each key.
    unique_by_key = {}
    duplicate_count = 0

    for job in candidates:
        key = dedupe_key(job)
        current = unique_by_key.get(key)

        if current is None:
            unique_by_key[key] = job
            continue

        duplicate_count += 1
        if get_score(job) > get_score(current):
            unique_by_key[key] = job

    unique_candidates = list(unique_by_key.values())

    experience_flagged = [
        job for job in unique_candidates
        if job.get("review_flag")
    ]

    # Keep the experience-review list separate, as in the original script.
    review_ready = [
        job for job in unique_candidates
        if not job.get("review_flag")
    ]

    aggregator_flagged = [
        job for job in review_ready
        if job.get("possible_aggregator_page")
    ]

    # These are excluded from the main vacancy rankings, but shown separately.
    likely_vacancies = [
        job for job in review_ready
        if not job.get("possible_aggregator_page")
    ]

    likely_vacancies.sort(key=sort_key)
    aggregator_flagged.sort(key=sort_key)
    experience_flagged.sort(key=sort_key)

    buckets = {
        "Frontend / React": [],
        "UI/UX Design": [],
        "Hybrid UI/UX + Frontend": [],
        "Other related roles": [],
    }

    for job in likely_vacancies:
        buckets[job["role_category"]].append(job)

    print("Read-only queue check completed.")
    print(f"Remote queue records: {len(remote_jobs)}")
    print(f"Valid HTTPS job records: {len(valid)}")
    print(f"Invalid records ignored: {invalid_count}")
    print(f"Filtered by existing role rules: {len(excluded)}")
    print(f"Duplicate records collapsed: {duplicate_count}")
    print(f"Unique candidates after deduplication: {len(unique_candidates)}")
    print(f"Flagged for 3+ years experience review: {len(experience_flagged)}")
    print(f"Candidates without a 3+ years flag: {len(review_ready)}")
    print(f"Possible aggregator/search pages flagged: {len(aggregator_flagged)}")
    print(f"Likely vacancy candidates in main rankings: {len(likely_vacancies)}")

    print("\nPriority counts (likely vacancies without a 3+ years flag):")
    for category, jobs in buckets.items():
        print(f"- {category}: {len(jobs)}")

    india_count = sum(
        1 for job in likely_vacancies
        if job["location_classification"] == "INDIA_OR_INDIA_REMOTE"
    )
    foreign_count = sum(
        1 for job in likely_vacancies
        if job["location_classification"] == "FOREIGN_LOCATION_REVIEW"
    )
    unclear_count = sum(
        1 for job in likely_vacancies
        if job["location_classification"] in (
            "LOCATION_UNCLEAR_REVIEW",
            "REMOTE_LOCATION_UNCLEAR",
        )
    )

    print("\nLocation classification:")
    print(f"- India or India-remote signal: {india_count}")
    print(f"- Foreign location needs eligibility review: {foreign_count}")
    print(f"- Unclear location needs review: {unclear_count}")

    unpaid_count = sum(
        1 for job in likely_vacancies
        if job["pay_classification"] == "UNPAID"
    )
    pay_mentioned_count = sum(
        1 for job in likely_vacancies
        if job["pay_classification"] == "PAY_MENTIONED_CHECK_DETAILS"
    )
    pay_unknown_count = sum(
        1 for job in likely_vacancies
        if job["pay_classification"] == "PAY_NOT_STATED"
    )

    print("\nPay classification:")
    print(f"- Unpaid or volunteer signals: {unpaid_count}")
    print(f"- Pay/stipend/salary mentioned; verify details: {pay_mentioned_count}")
    print(f"- Pay not stated: {pay_unknown_count}")

    print("\nTop overall likely vacancies:")
    display_candidates(likely_vacancies, limit=10)

    for category in (
        "Frontend / React",
        "UI/UX Design",
        "Hybrid UI/UX + Frontend",
    ):
        print(f"\nTop {category} likely vacancies (up to 10):")
        display_candidates(buckets[category], limit=10)

    print("\nPossible job-search/aggregator pages (manual review):")
    display_candidates(aggregator_flagged, limit=10)

    print("\nExamples flagged for experience review (not auto-rejected):")
    for job in experience_flagged[:5]:
        title = safe_log(job.get("title") or job.get("job_title"))
        years = job.get("review_experience_years")
        score = job.get("suitability_score", 0)
        print(f"- experience={years}+ years | score={score} | {title}")

    print(
        "\nSafety: read-only queue access; no queue/tracker files "
        "written; no browser launched; no applications submitted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
