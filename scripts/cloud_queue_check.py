```python
"""Read-only cloud check of the job-alert queue with ranking,
deduplication, role categories, and experience-review flags.
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


def safe_log(value, limit=180):
    """Keep printed output safe for GitHub Actions logs."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit]
    return " " + text if text.startswith("::") else text


def plain_text(value):
    """Remove HTML markup and normalize whitespace."""
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_queue(token):
    """Fetch the source queue using read-only GitHub API access."""
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
    """Normalize titles for duplicate detection."""
    title = plain_text(job.get("title") or job.get("job_title"))

    title = re.sub(
        r"\s*[|–—-]\s*(?:LinkedIn Jobs?.*|Indeed.*|Naukri\.com.*)$",
        "",
        title,
        flags=re.IGNORECASE,
    )

    return re.sub(r"\s+", " ", title).strip().casefold()


def dedupe_key(job):
    """Build a conservative key without treating all jobs as identical."""
    title = normalized_title(job)
    location = plain_text(job.get("location")).casefold()
    snippet = str(job.get("snippet") or "")

    # Use a destination URL from the snippet when one is present.
    match = re.search(
        r"""href=["'](https?://[^"']+)["']""",
        snippet,
        flags=re.IGNORECASE,
    )

    candidate_url = match.group(1) if match else str(job.get("url") or "")
    parsed = urllib.parse.urlsplit(candidate_url)

    # Google News wrappers can vary for the same listing.
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
    """Flag likely 3+ year requirements for human review, not rejection."""
    title = plain_text(job.get("title") or job.get("job_title"))

    description = " ".join(
        plain_text(job.get(key))
        for key in ("snippet", "description", "experience", "seniority")
    )

    title_years = extract_min_experience_years(title)
    description_years = extract_min_experience_years(description)

    years = (
        title_years
        if title_years is not None
        else description_years
    )

    if years is not None and years >= 3:
        return years, "REVIEW_EXPERIENCE_3_PLUS_YEARS"

    return years, None


def role_bucket(job):
    """Group candidates into the user's target role categories."""
    title = plain_text(
        job.get("title") or job.get("job_title")
    ).casefold()

    # Normalize common UI/UX spelling variations.
    title = re.sub(
        r"\bui\s*[/\-–—]\s*ux\b",
        "ui ux",
        title,
    )
    title = re.sub(r"\s+", " ", title).strip()

    # Check hybrid roles before individual design/development roles.
    hybrid_terms = (
        "ui ux developer",
        "ui ux engineer",
        "ui developer",
        "ux developer",
        "design technologist",
        "frontend designer",
        "front end designer",
        "ui ux frontend",
        "ui ux front end",
    )

    if any(term in title for term in hybrid_terms):
        return "Hybrid UI/UX + Frontend"

    design_terms = (
        "ui ux designer",
        "ui designer",
        "ux designer",
        "product designer",
        "ui ux specialist",
        "user experience designer",
        "user interface designer",
        "ux ui designer",
        "visual designer",
        "interaction designer",
    )

    if any(term in title for term in design_terms):
        return "UI/UX Design"

    frontend_terms = (
        "react developer",
        "react engineer",
        "react.js",
        "react js developer",
        "frontend developer",
        "frontend engineer",
        "front-end developer",
        "front-end engineer",
        "front end developer",
        "front end engineer",
        "web developer",
        "javascript developer",
        "frontend",
    )

    if any(term in title for term in frontend_terms):
        return "Frontend / React"

    return "Other related roles"


def get_score(job):
    """Return a numeric score even if the source value is missing."""
    try:
        return float(job.get("suitability_score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def display_candidates(jobs, limit=10):
    """Print a short list of candidates for manual review."""
    for job in jobs[:limit]:
        title = safe_log(
            job.get("title") or job.get("job_title")
        )
        score = job.get("suitability_score", 0)
        reason = safe_log(job.get("suitability_reason", ""))

        if reason:
            print(f"- score={score} | {title} | {reason}")
        else:
            print(f"- score={score} | {title}")

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

    except (
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(
            "ERROR: Queue response is invalid "
            f"({type(exc).__name__})."
        )
        return 1

    valid = []
    invalid_count = 0

    # Validate each record and apply the existing role rules.
    for job in remote_jobs:
        if not isinstance(job, dict):
            invalid_count += 1
            continue

        title = str(
            job.get("title") or job.get("job_title") or ""
        ).strip()

        url = str(job.get("url") or "").strip()

        if not title or not url.startswith("https://"):
            invalid_count += 1
            continue

        evaluated = evaluate_job(job)
        years, flag = experience_flag(evaluated)

        evaluated["review_experience_years"] = years
        evaluated["review_flag"] = flag

        valid.append(evaluated)

    excluded = [
        job for job in valid
        if job.get("status") == "Skipped"
    ]

    candidates = [
        job for job in valid
        if job.get("status") != "Skipped"
    ]

    # Deduplicate and retain the highest-scoring record.
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
    unique_candidates.sort(
        key=get_score,
        reverse=True,
    )

    experience_flagged = [
        job for job in unique_candidates
        if job.get("review_flag")
    ]

    # Keep flagged jobs visible in the separate review section.
    # Do not automatically reject them.
    review_ready = [
        job for job in unique_candidates
        if not job.get("review_flag")
    ]

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------
    print("Read-only queue check completed.")
    print(f"Remote queue records: {len(remote_jobs)}")
    print(f"Valid HTTPS job records: {len(valid)}")
    print(f"Invalid records ignored: {invalid_count}")
    print(f"Filtered by existing role rules: {len(excluded)}")
    print(f"Duplicate records collapsed: {duplicate_count}")
    print(
        "Unique candidates after deduplication: "
        f"{len(unique_candidates)}"
    )
    print(
        "Flagged for 3+ years experience review: "
        f"{len(experience_flagged)}"
    )
    print(
        "Candidates without a 3+ years flag: "
        f"{len(review_ready)}"
    )

    # ---------------------------------------------------------
    # Categorize the candidates that do not have the 3+ years flag.
    # Existing suitability scores are preserved.
    # ---------------------------------------------------------
    buckets = {
        "Frontend / React": [],
        "UI/UX Design": [],
        "Hybrid UI/UX + Frontend": [],
        "Other related roles": [],
    }

    for job in review_ready:
        category = role_bucket(job)
        buckets[category].append(job)

    print(
        "\nPriority counts for candidates without "
        "a 3+ years flag:"
    )

    for category, jobs in buckets.items():
        print(f"- {category}: {len(jobs)}")

    # ---------------------------------------------------------
    # Overall top 10
    # ---------------------------------------------------------
    print(
        "\nTop overall candidates "
        "(title, score, and matching signals):"
    )
    display_candidates(review_ready, limit=10)

    # ---------------------------------------------------------
    # Separate top 10 for each target role family
    # ---------------------------------------------------------
    priority_categories = (
        "Frontend / React",
        "UI/UX Design",
        "Hybrid UI/UX + Frontend",
    )

    for category in priority_categories:
        print(f"\nTop {category} candidates (up to 10):")
        display_candidates(buckets[category], limit=10)

    # ---------------------------------------------------------
    # Experience review: flagged jobs are not auto-rejected.
    # ---------------------------------------------------------
    print(
        "\nExamples flagged for experience review "
        "(not auto-rejected):"
    )

    for job in experience_flagged[:5]:
        title = safe_log(
            job.get("title") or job.get("job_title")
        )
        years = job.get("review_experience_years")
        score = job.get("suitability_score", 0)

        print(
            f"- experience={years}+ years | "
            f"score={score} | {title}"
        )

    print(
        "\nSafety: read-only queue access; no queue/tracker files "
        "written; no browser launched; no applications submitted."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
```
