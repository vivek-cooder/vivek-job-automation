
"""Read-only cloud check of the job-alert queue with deduplication and experience flags."""

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
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit]
    return " " + text if text.startswith("::") else text


def plain_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


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

    # Remove common source suffixes from job titles.
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

    # Google News wrappers often change for the same listing.
    # In that case, compare normalized title and location instead.
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
    """Flag likely 3+ year requirements for human review; do not auto-reject."""
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
            f"ERROR: Queue response is invalid "
            f"({type(exc).__name__})."
        )
        return 1

    valid = []
    invalid_count = 0

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

    # Collapse duplicates and retain the highest-scoring record.
    unique_by_key = {}
    duplicate_count = 0

    for job in candidates:
        key = dedupe_key(job)
        current = unique_by_key.get(key)

        if current is None:
            unique_by_key[key] = job
            continue

        duplicate_count += 1

        new_score = float(job.get("suitability_score", 0) or 0)
        old_score = float(current.get("suitability_score", 0) or 0)

        if new_score > old_score:
            unique_by_key[key] = job

    unique_candidates = list(unique_by_key.values())
    unique_candidates.sort(
        key=lambda job: float(
            job.get("suitability_score", 0) or 0
        ),
        reverse=True,
    )

    experience_flagged = [
        job for job in unique_candidates
        if job.get("review_flag")
    ]

    review_ready = [
        job for job in unique_candidates
        if not job.get("review_flag")
    ]

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

    print("\nTop candidates for human review (title and score only):")

    for job in review_ready[:10]:
        title = safe_log(
            job.get("title") or job.get("job_title")
        )
        score = job.get("suitability_score", 0)
        print(f"- score={score} | {title}")

    print("\nExamples flagged for experience review (not auto-rejected):")

    for job in experience_flagged[:5]:
        title = safe_log(
            job.get("title") or job.get("job_title")
        )
        years = job.get("review_experience_years")
        print(f"- experience={years}+ years | {title}")

    print(
        "\nSafety: read-only queue access; no queue/tracker files "
        "written; no browser launched; no applications submitted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
