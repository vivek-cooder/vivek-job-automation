
"""Read-only cloud check of the job-alert queue."""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

from worker.sync_queue import evaluate_job

API_URL = (
    "https://api.github.com/repos/vivek-cooder/vivek-job-alert/"
    "contents/data/application_queue.json?ref=main"
)


def safe_log(value, limit=180):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit]
    return " " + text if text.startswith("::") else text


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

    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise ValueError("GitHub API response did not contain file content.")

    raw = base64.b64decode(payload["content"]).decode("utf-8")
    jobs = json.loads(raw)

    if not isinstance(jobs, list):
        raise ValueError("The remote queue must be a JSON list.")

    return jobs


def main():
    token = os.environ.get("JOB_ALERTS_READ_TOKEN", "").strip()
    if not token:
        print("ERROR: Repository secret JOB_ALERTS_READ_TOKEN is missing.")
        return 2

    try:
        remote_jobs = fetch_queue(token)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: GitHub returned HTTP {exc.code} while reading the queue.")
        return 1
    except (urllib.error.URLError, TimeoutError):
        print("ERROR: Could not reach GitHub to read the queue.")
        return 1
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: Queue response is invalid ({type(exc).__name__}).")
        return 1

    valid = []
    invalid_count = 0

    for job in remote_jobs:
        if not isinstance(job, dict):
            invalid_count += 1
            continue

        title = str(job.get("title") or job.get("job_title") or "").strip()
        url = str(job.get("url") or "").strip()

        if not title or not url.startswith("https://"):
            invalid_count += 1
            continue

        valid.append(evaluate_job(job))

    excluded = [job for job in valid if job.get("status") == "Skipped"]
    candidates = [job for job in valid if job.get("status") != "Skipped"]
    candidates.sort(
        key=lambda job: float(job.get("suitability_score", 0) or 0),
        reverse=True,
    )

    print("Read-only queue check completed.")
    print(f"Remote queue records: {len(remote_jobs)}")
    print(f"Valid HTTPS job records: {len(valid)}")
    print(f"Invalid records ignored: {invalid_count}")
    print(f"Filtered by existing role rules: {len(excluded)}")
    print(f"Candidates for human review: {len(candidates)}")
    print("Top candidates (title and score only):")

    for job in candidates[:10]:
        title = safe_log(job.get("title") or job.get("job_title"))
        print(f"- score={job.get('suitability_score', 0)} | {title}")

    print(
        "Safety: no queue/tracker files written; no browser launched; "
        "no applications submitted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
