"""Pure offline unit and safety regression tests for queue management and scoring.

Covers:
- Validation and safe handling of empty / malformed job records.
- Durable queue file loading, corruption resilience, and atomic writes.
- Job deduplication rules: URL canonicalization, ID priority, identity keys, fallback matching.
- Distinct jobs: verification that unique jobs are never falsely flagged as duplicates.
- Protected duplicates: preservation of active, waiting, and terminal jobs.
- State machine transitions, monotonic status protections, and retry counting.
- Suitability scoring: role weighting, skill signals, location, experience, and negative penalties.
- Stale active job recovery: threshold timing, retry exhaustion, and non-active immunity.
- Alert repository sync safety: duplicate handling, tracker coordination, and non-destructive updates.
- Complete file isolation: temporary directories only, zero modifications to persistent project data.

ZERO browser launches, ZERO Playwright imports, ZERO network or socket activity.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from tests.data_safety import preserve_project_data_test
from worker.job_queue import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    WAITING_STATUSES,
    add_job,
    canonicalize_url,
    find_duplicate,
    find_protected_queue_duplicate,
    get_job_identity_keys,
    get_job_key,
    get_next_job,
    get_waiting_job,
    load_queue,
    mark_job_status,
    normalize_suitability_score,
    normalize_text,
    recover_stale_jobs,
    validate_job,
    write_queue,
)
from worker.sync_queue import (
    evaluate_job,
    extract_min_experience_years,
    score_job,
    sync_jobs,
)
from worker.tracker import is_duplicate_application, load_applications, save_application


class QueueSafetyOfflineBase(unittest.TestCase):
    """Base fixture providing isolated temporary directory and synthetic records."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="test_queue_safety_")
        self.temp_dir_path = Path(self._temp_dir.name)
        self.queue_path = self.temp_dir_path / "application_queue.json"
        self.tracker_path = self.temp_dir_path / "applications.json"

    def tearDown(self):
        self._temp_dir.cleanup()

    def make_job(self, **overrides):
        """Generate a valid synthetic job record with optional overrides."""
        record = {
            "id": "alert-test-001",
            "title": "React Developer",
            "company": "Test Dynamics",
            "source": "GitHub Alerts",
            "url": "https://jobs.example.test/roles/101/?ref=daily_digest",
            "location": "Mumbai",
            "status": "Queued",
        }
        record.update(overrides)
        return record


class TestJobValidationOffline(QueueSafetyOfflineBase):
    """Validates boundary handling and rejection of malformed job records."""

    @preserve_project_data_test
    def test_non_dict_job_record_rejected(self):
        valid, reason = validate_job("not-a-dict")
        self.assertFalse(valid)
        self.assertEqual(reason, "JOB_RECORD_INVALID")

        valid, reason = validate_job(None)
        self.assertFalse(valid)
        self.assertEqual(reason, "JOB_RECORD_INVALID")

        valid, reason = validate_job([{"title": "Dev"}])
        self.assertFalse(valid)
        self.assertEqual(reason, "JOB_RECORD_INVALID")

    @preserve_project_data_test
    def test_missing_or_blank_title_rejected(self):
        valid, reason = validate_job({"url": "https://example.test/job/1"})
        self.assertFalse(valid)
        self.assertEqual(reason, "JOB_TITLE_MISSING")

        valid, reason = validate_job({"title": "   ", "url": "https://example.test/job/1"})
        self.assertFalse(valid)
        self.assertEqual(reason, "JOB_TITLE_MISSING")

    @preserve_project_data_test
    def test_missing_or_blank_url_rejected(self):
        valid, reason = validate_job({"title": "React Developer"})
        self.assertFalse(valid)
        self.assertEqual(reason, "JOB_URL_MISSING")

        valid, reason = validate_job({"title": "React Developer", "url": ""})
        self.assertFalse(valid)
        self.assertEqual(reason, "JOB_URL_MISSING")

    @preserve_project_data_test
    def test_invalid_url_scheme_rejected(self):
        invalid_schemes = [
            "ftp://example.test/job/1",
            "javascript:void(0)",
            "data:text/html,<div>Job</div>",
            "custom-app://open/job",
            "just-a-string-without-scheme",
            "HTTPS://example.test/job/1",
            "HTTP://example.test/job/1",
        ]
        for url in invalid_schemes:
            valid, reason = validate_job({"title": "React Developer", "url": url})
            self.assertFalse(valid, f"Expected {url} to be rejected")
            self.assertEqual(reason, "JOB_URL_INVALID")

    @preserve_project_data_test
    def test_valid_schemes_accepted(self):
        valid_urls = [
            "http://example.test/job/1",
            "https://example.test/job/1?q=dev",
            "file:///path/to/test_application.html",
        ]
        for url in valid_urls:
            valid, reason = validate_job({"title": "React Developer", "url": url})
            self.assertTrue(valid, f"Expected {url} to be accepted")
            self.assertIsNone(reason)


class TestQueueFileRobustnessOffline(QueueSafetyOfflineBase):
    """Validates queue reading, corruption handling, and atomic writes."""

    @preserve_project_data_test
    def test_load_queue_nonexistent_file_returns_empty_list(self):
        missing = self.temp_dir_path / "does_not_exist.json"
        self.assertEqual(load_queue(missing), [])

    @preserve_project_data_test
    def test_load_queue_empty_file_returns_empty_list(self):
        self.queue_path.write_text("", encoding="utf-8")
        self.assertEqual(load_queue(self.queue_path), [])

    @preserve_project_data_test
    def test_load_queue_corrupt_json_returns_empty_list(self):
        self.queue_path.write_text("{this is not: valid json [}", encoding="utf-8")
        self.assertEqual(load_queue(self.queue_path), [])

    @preserve_project_data_test
    def test_load_queue_non_list_root_returns_empty_list(self):
        self.queue_path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        self.assertEqual(load_queue(self.queue_path), [])

        self.queue_path.write_text(json.dumps(12345), encoding="utf-8")
        self.assertEqual(load_queue(self.queue_path), [])

    @preserve_project_data_test
    def test_atomic_queue_write_and_round_trip(self):
        sample_jobs = [
            self.make_job(id="job-1", title="Frontend Engineer"),
            self.make_job(id="job-2", title="React Specialist"),
        ]
        write_queue(sample_jobs, self.queue_path)
        loaded = load_queue(self.queue_path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["id"], "job-1")
        self.assertEqual(loaded[1]["id"], "job-2")


class TestJobDeduplicationOffline(QueueSafetyOfflineBase):
    """Validates duplicate detection, identity resolution, and idempotency."""

    @preserve_project_data_test
    def test_new_job_is_queued_and_duplicate_insert_is_idempotent(self):
        job = self.make_job()
        added, record, error = add_job(job, self.queue_path)
        self.assertTrue(added)
        self.assertEqual(record["status"], "Queued")
        self.assertIn("job_key", record)
        self.assertEqual(record["retry_count"], 0)

        # Duplicate addition attempt
        again, existing, error = add_job(job, self.queue_path)
        self.assertFalse(again)
        self.assertEqual(error, "DUPLICATE_JOB")
        self.assertEqual(existing["id"], "alert-test-001")
        self.assertEqual(len(load_queue(self.queue_path)), 1)

    @preserve_project_data_test
    def test_same_url_with_changed_id_is_detected_as_duplicate(self):
        add_job(self.make_job(id="first-id"), self.queue_path)
        added, _, reason = add_job(
            self.make_job(id="second-id", url="https://jobs.example.test/roles/101/?ref=daily_digest"),
            self.queue_path,
        )
        self.assertFalse(added)
        self.assertEqual(reason, "DUPLICATE_JOB")
        self.assertEqual(len(load_queue(self.queue_path)), 1)

    @preserve_project_data_test
    def test_same_id_with_changed_url_is_detected_as_duplicate(self):
        add_job(self.make_job(id="same-alert-id", url="https://jobs.example.test/roles/101/"), self.queue_path)
        added, _, reason = add_job(
            self.make_job(id="same-alert-id", url="https://jobs.example.test/roles/999/"),
            self.queue_path,
        )
        self.assertFalse(added)
        self.assertEqual(reason, "DUPLICATE_JOB")
        self.assertEqual(len(load_queue(self.queue_path)), 1)

    @preserve_project_data_test
    def test_url_canonicalization_variations_match_as_duplicates(self):
        canonical = "https://jobs.example.test/roles/101?ref=digest"
        variants = [
            "https://jobs.example.test/roles/101/?ref=digest",
            "https://JOBS.EXAMPLE.TEST/roles/101?ref=digest",
            "https://jobs.example.test/roles/101?ref=digest#section-anchor",
            "  https://jobs.example.test/roles/101?ref=digest  ",
        ]
        added, _, _ = add_job(self.make_job(id=None, url=canonical), self.queue_path)
        self.assertTrue(added)
        for variant in variants:
            added, _, reason = add_job(self.make_job(id=None, url=variant), self.queue_path)
            self.assertFalse(added, f"Variant {variant} should match canonical {canonical}")
            self.assertEqual(reason, "DUPLICATE_JOB")
        self.assertEqual(len(load_queue(self.queue_path)), 1)

    @preserve_project_data_test
    def test_fallback_identity_by_source_company_and_title(self):
        first = self.make_job(id=None, url="", source="BoardX", company="Acme Corp", title="UI Designer")
        second = dict(first, title="  ui   designer ")
        self.assertEqual(get_job_key(first), get_job_key(second))
        self.assertTrue(get_job_key(first).startswith("job:boardx|acme corp|ui designer"))

    @preserve_project_data_test
    def test_distinct_jobs_are_not_flagged_as_duplicates(self):
        add_job(self.make_job(id="job-alpha", url="https://jobs.example.test/roles/alpha"), self.queue_path)

        # Same company & title, different URL
        added, _, reason = add_job(
            self.make_job(id="job-beta", url="https://jobs.example.test/roles/beta"),
            self.queue_path,
        )
        self.assertTrue(added, reason)

        # Different company, different URL
        added, _, reason = add_job(
            self.make_job(id="job-gamma", company="Other Corp", url="https://jobs.example.test/roles/gamma"),
            self.queue_path,
        )
        self.assertTrue(added, reason)

        self.assertEqual(len(load_queue(self.queue_path)), 3)

    @preserve_project_data_test
    def test_empty_or_identityless_records_do_not_falsely_match(self):
        self.assertEqual(get_job_key({}), "")
        self.assertEqual(get_job_identity_keys({}), [])
        existing = [self.make_job()]
        self.assertIsNone(find_duplicate({}, existing))


class TestProtectedQueueDuplicatesOffline(QueueSafetyOfflineBase):
    """Validates protection of active, waiting, and completed queue jobs from overwrite."""

    @preserve_project_data_test
    def test_active_and_waiting_statuses_are_protected(self):
        incoming = self.make_job(id="incoming-id", status="Queued")

        # Protected active states
        for active_status in ACTIVE_STATUSES:
            active_job = self.make_job(id="existing-id", status=active_status)
            found = find_protected_queue_duplicate(incoming, [active_job])
            self.assertEqual(found, active_job, f"Expected {active_status} to be protected")

        # Protected waiting states
        for waiting_status in WAITING_STATUSES:
            waiting_job = self.make_job(id="existing-id", status=waiting_status)
            found = find_protected_queue_duplicate(incoming, [waiting_job])
            self.assertEqual(found, waiting_job, f"Expected {waiting_status} to be protected")

    @preserve_project_data_test
    def test_terminal_submitted_and_skipped_are_protected(self):
        incoming = self.make_job(id="incoming-id", status="Queued")

        submitted = self.make_job(id="existing-id", status="Submitted")
        self.assertEqual(find_protected_queue_duplicate(incoming, [submitted]), submitted)

        skipped = self.make_job(id="existing-id", status="Skipped")
        self.assertEqual(find_protected_queue_duplicate(incoming, [skipped]), skipped)

    @preserve_project_data_test
    def test_failed_queued_and_retry_are_not_protected(self):
        incoming = self.make_job(id="incoming-id", status="Queued")

        unprotected_statuses = ["Failed", "Queued", "Retry"]
        for status in unprotected_statuses:
            job = self.make_job(id="existing-id", status=status)
            self.assertIsNone(
                find_protected_queue_duplicate(incoming, [job]),
                f"Status {status} should not be treated as a protected duplicate",
            )

    @preserve_project_data_test
    def test_protected_duplicate_ignores_same_id(self):
        incoming = self.make_job(id="same-id", status="Queued")
        same_id_submitted = self.make_job(id="same-id", status="Submitted")
        self.assertIsNone(find_protected_queue_duplicate(incoming, [same_id_submitted]))


class TestStatusTransitionsAndMonotonicityOffline(QueueSafetyOfflineBase):
    """Validates state machine safety, monotonic terminal protections, and queries."""

    @preserve_project_data_test
    def test_terminal_statuses_cannot_regress(self):
        add_job(self.make_job(id="job-terminal"), self.queue_path)
        mark_job_status("job-terminal", "Submitted", path=self.queue_path)

        # Attempt to regress to active or queued states
        for invalid_regression in ("Queued", "Started", "Processing", "Retry"):
            updated = mark_job_status("job-terminal", invalid_regression, path=self.queue_path)
            self.assertEqual(updated["status"], "Submitted")

        loaded = load_queue(self.queue_path)[0]
        self.assertEqual(loaded["status"], "Submitted")
        self.assertIn("completed_at", loaded)

    @preserve_project_data_test
    def test_waiting_statuses_cannot_regress_to_queued_or_started(self):
        add_job(self.make_job(id="job-waiting"), self.queue_path)
        mark_job_status("job-waiting", "WaitingForHuman", path=self.queue_path)

        for invalid_regression in ("Queued", "Started"):
            updated = mark_job_status("job-waiting", invalid_regression, path=self.queue_path)
            self.assertEqual(updated["status"], "WaitingForHuman")

        loaded = load_queue(self.queue_path)[0]
        self.assertEqual(loaded["status"], "WaitingForHuman")

    @preserve_project_data_test
    def test_active_transition_sets_started_at(self):
        add_job(self.make_job(id="job-progress"), self.queue_path)
        updated = mark_job_status("job-progress", "Processing", path=self.queue_path)
        self.assertEqual(updated["status"], "Processing")
        self.assertIn("started_at", updated)
        self.assertIn("updated_at", updated)

    @preserve_project_data_test
    def test_get_next_and_waiting_job_queries(self):
        added1, _, _ = add_job(
            self.make_job(id="j1", url="https://jobs.example.test/roles/1", status="Failed"),
            self.queue_path,
        )
        self.assertTrue(added1)

        added2, _, _ = add_job(
            self.make_job(id="j2", url="https://jobs.example.test/roles/2", status="WaitingForHuman"),
            self.queue_path,
        )
        self.assertTrue(added2)

        added3, _, _ = add_job(
            self.make_job(id="j3", url="https://jobs.example.test/roles/3", status="Queued"),
            self.queue_path,
        )
        self.assertTrue(added3)

        self.assertEqual(len(load_queue(self.queue_path)), 3)

        waiting = get_waiting_job(self.queue_path)
        self.assertIsNotNone(waiting)
        self.assertEqual(waiting["id"], "j2")

        next_job = get_next_job(self.queue_path)
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["id"], "j3")


class TestSuitabilityScoringOffline(QueueSafetyOfflineBase):
    """Validates scoring heuristics, boundary cases, and role filtering."""

    @preserve_project_data_test
    def test_target_high_priority_role_scoring(self):
        job = self.make_job(
            title="React Developer",
            snippet="Hands-on JavaScript, web developer skills",
            location="Mumbai",
            experience="Entry level, 0-1 years",
        )
        score, reason = score_job(job)
        # Expected: target role (+5) + JS skills (+3) + location (+2) + entry level (+3) = +13
        self.assertGreaterEqual(score, 13)
        self.assertIn("target role +5", reason)
        self.assertIn("preferred location +2", reason)
        self.assertIn("entry level +3", reason)

    @preserve_project_data_test
    def test_related_role_and_remote_scoring(self):
        job = self.make_job(
            title="Frontend Specialist",
            snippet="React component development",
            location="Remote",
            experience="0-2 years",
        )
        score, reason = score_job(job)
        # Expected: related role (+3) + remote (+3) + 0-2 years (+2) = 8
        self.assertGreaterEqual(score, 8)
        self.assertIn("related role +3", reason)
        self.assertIn("remote +3", reason)
        self.assertIn("0–2 years +2", reason)

    @preserve_project_data_test
    def test_ui_ux_title_normalization_matches_high_priority(self):
        titles = [
            "UI/UX Designer",
            "UI / UX Designer",
            "UI-UX Designer",
            "UI – UX Designer",
        ]
        for title in titles:
            score, reason = score_job(self.make_job(title=title))
            self.assertGreaterEqual(score, 5, f"Title '{title}' failed to match high priority")
            self.assertIn("target role +5", reason)

    @preserve_project_data_test
    def test_clearly_unrelated_roles_receive_penalty_and_are_skipped(self):
        unrelated_titles = [
            "Accountant",
            "Sales Representative",
            "Marketing Specialist",
            "Civil Engineer",
            "HR Recruiter",
            "Data Entry Operator",
            "Driver",
        ]
        for title in unrelated_titles:
            job = self.make_job(title=title, snippet="Accounting and sales ledger")
            score, reason = score_job(job)
            self.assertLess(score, 0)
            self.assertIn("clearly unrelated role -10", reason)

            evaluated = evaluate_job(job)
            self.assertEqual(evaluated.get("status"), "Skipped")
            self.assertEqual(evaluated.get("skip_reason"), "UNRELATED_ROLE")

    @preserve_project_data_test
    def test_borderline_roles_are_retained_not_skipped(self):
        borderline_titles = [
            "Product Specialist",
            "Technical Operations Analyst",
            "Support Coordinator",
        ]
        for title in borderline_titles:
            evaluated = evaluate_job(self.make_job(title=title))
            self.assertNotEqual(evaluated.get("status"), "Skipped")
            self.assertNotIn("skip_reason", evaluated)

    @preserve_project_data_test
    def test_senior_penalty_applied_for_senior_titles_with_high_experience(self):
        job = self.make_job(
            title="Senior React Developer",
            snippet="React.js development with 5+ years required",
        )
        score, reason = score_job(job)
        self.assertIn("senior experience requirement -7", reason)

    @preserve_project_data_test
    def test_empty_or_none_fields_handled_safely(self):
        empty_job = {}
        score, reason = score_job(empty_job)
        self.assertEqual(score, 0)
        self.assertEqual(reason, "no strong profile signal; retained for review")

        evaluated = evaluate_job(empty_job)
        self.assertEqual(evaluated["suitability_score"], 0)
        self.assertEqual(evaluated["suitability_reason"], "no strong profile signal; retained for review")

        none_fields_job = {
            "title": None,
            "snippet": None,
            "description": None,
            "location": None,
            "experience": None,
        }
        score, reason = score_job(none_fields_job)
        self.assertEqual(score, 0)

    @preserve_project_data_test
    def test_equal_base_priority_for_frontend_and_ui_ux(self):
        titles = [
            "Frontend Developer",
            "Frontend Engineer",
            "React Developer",
            "React Engineer",
            "UI/UX Designer",
            "UI Designer",
            "UX Designer",
            "Product Designer",
            "Design Technologist",
        ]
        for title in titles:
            score, reason = score_job(self.make_job(title=title, location=""))
            self.assertEqual(score, 5, f"Title '{title}' failed to receive equal base priority 5")
            self.assertIn("target role +5", reason)

    @preserve_project_data_test
    def test_hybrid_title_awards_single_tier_bonus(self):
        hybrid_titles = [
            "UI/UX Designer & Frontend Developer",
            "UI/UX Designer & Software Engineer",
            "UI/UX Developer",
            "UI Developer",
            "UX Developer",
            "UI Engineer",
        ]
        for title in hybrid_titles:
            score, reason = score_job(self.make_job(title=title, location=""))
            self.assertEqual(score, 5, f"Hybrid title '{title}' received double bonus: score={score}")
            self.assertIn("target role +5", reason)
            self.assertNotIn("related role +3", reason)

    @preserve_project_data_test
    def test_related_role_scoring(self):
        related_titles = [
            "Junior Software Developer",
            "Junior Software Engineer",
            "Junior Developer",
            "Software Developer",
            "Software Engineer",
            "Associate Software Engineer",
            "Associate Developer",
        ]
        for title in related_titles:
            score, reason = score_job(self.make_job(title=title, location=""))
            self.assertEqual(score, 3, f"Title '{title}' failed to receive related role bonus 3")
            self.assertIn("related role +3", reason)

    @preserve_project_data_test
    def test_plain_software_engineer_receives_related_role_bonus(self):
        job = self.make_job(title="Software Engineer", location="", snippet="")
        score, reason = score_job(job)
        self.assertEqual(score, 3)
        self.assertEqual(reason, "related role +3")

    @preserve_project_data_test
    def test_experience_parsing_ranges_and_boundary_cases(self):
        cases = [
            ("2–5 years", 2),
            ("2-5 years", 2),
            ("3–5 years", 3),
            ("3 to 5 yrs", 3),
            ("5+ years", 5),
            ("7+ yrs", 7),
            ("at least 3 years", 3),
            ("minimum 4 years", 4),
            ("min 5 years", 5),
            ("3 years of relevant experience", 3),
            ("4 years of experience", 4),
            ("0–2 years", 0),
            ("0-2 years", 0),
            ("2.5 years of experience", 2.5),
            ("2.5+ years of experience", 2.5),
            ("1.5–3 years of experience", 1.5),
            ("1.5 to 3 yrs", 1.5),
            ("2.5+ years", 2.5),
            ("at least 2.5 years", 2.5),
            ("minimum 1.5 yrs", 1.5),
            ("12+ years in business", None),
        ]
        for text, expected in cases:
            parsed = extract_min_experience_years(text)
            self.assertEqual(parsed, expected, f"Failed for '{text}': expected {expected}, got {parsed}")

        # Specific regression checks:
        self.assertEqual(extract_min_experience_years("2.5 years of experience"), 2.5)
        self.assertNotEqual(extract_min_experience_years("2.5 years of experience"), 5)
        self.assertIsInstance(extract_min_experience_years("2.5 years of experience"), float)
        self.assertEqual(extract_min_experience_years("2.5+ years of experience"), 2.5)
        self.assertEqual(extract_min_experience_years("1.5–3 years of experience"), 1.5)

        self.assertIsNone(extract_min_experience_years(None))
        self.assertIsNone(extract_min_experience_years(""))
        self.assertIsNone(extract_min_experience_years("No years mentioned here"))

    @preserve_project_data_test
    def test_senior_penalties_based_on_minimum_experience(self):
        # 1. Senior role with 2-5 years: minimum is 2 (< 3), mild -2 penalty applies
        job_2_5 = self.make_job(
            title="Senior React Developer",
            snippet="React.js development with 2–5 years experience required",
            location="",
        )
        score, reason = score_job(job_2_5)
        # target (5) + skills (3) - mild senior (2) = 6
        self.assertEqual(score, 6)
        self.assertIn("senior title keyword -2", reason)

        # 1b. Senior role with 2.5 years: minimum is 2.5 (< 3), mild -2 penalty applies (not -7)
        job_2_5_dec = self.make_job(
            title="Senior React Developer",
            snippet="React.js development with 2.5 years of experience required",
            location="",
        )
        score, reason = score_job(job_2_5_dec)
        # target (5) + skills (3) - mild senior (2) = 6
        self.assertEqual(score, 6)
        self.assertIn("senior title keyword -2", reason)
        self.assertNotIn("senior experience requirement -7", reason)

        # 2. Senior role with 3-5 years: minimum is 3 (>= 3), harsh -7 penalty applies
        job_3_5 = self.make_job(
            title="Senior React Developer",
            snippet="React.js development with 3–5 years experience required",
            location="",
        )
        score, reason = score_job(job_3_5)
        # target (5) + skills (3) - senior exp (7) = 1
        self.assertEqual(score, 1)
        self.assertIn("senior experience requirement -7", reason)

        # 3. Senior role with 5+ years: minimum is 5 (>= 3), harsh -7 penalty applies
        job_5_plus = self.make_job(
            title="Senior Frontend Engineer",
            snippet="5+ years required in web development",
            location="",
        )
        score, reason = score_job(job_5_plus)
        self.assertIn("senior experience requirement -7", reason)

        # 4. Lead UI/UX with unstated experience: mild -2 penalty applies
        lead_job = self.make_job(
            title="Lead UI/UX Designer",
            snippet="Figma prototyping and user research",
            location="",
        )
        score, reason = score_job(lead_job)
        # target (5) + skills (3) - mild senior (2) = 6
        self.assertEqual(score, 6)
        self.assertIn("senior title keyword -2", reason)

        # 5. Junior title mentioning senior colleagues: NO senior penalty applied
        junior_job = self.make_job(
            title="Junior Frontend Developer",
            snippet="Will collaborate closely with Senior Developers with 5+ years experience",
            location="",
        )
        score, reason = score_job(junior_job)
        # target (5) + entry level (3) = 8
        self.assertNotIn("senior", reason)

    @preserve_project_data_test
    def test_it_support_exclusions_safe_and_title_scoped(self):
        # Positive exclusions: actual support titles must be excluded and skipped
        support_titles = [
            "IT Support Specialist",
            "Helpdesk Technician",
            "Desktop Support Engineer",
            "Application Support Analyst",
            "Service Desk Executive",
            "Technical Support Specialist",
            "Systems Administrator",
        ]
        for title in support_titles:
            job = self.make_job(title=title, snippet="Supporting corporate workstations")
            score, reason = score_job(job)
            self.assertLess(score, 0)
            self.assertIn("excluded support role -10", reason)

            evaluated = evaluate_job(job)
            self.assertEqual(evaluated.get("status"), "Skipped")
            self.assertEqual(evaluated.get("skip_reason"), "EXCLUDED_SUPPORT_ROLE")

        # Negative exclusions: developer/designer titles mentioning support in text must NOT be skipped
        safe_cases = [
            ("Frontend Developer", "Provide technical support to marketing teams on web portals"),
            ("React Developer", "Support legacy React applications and build modern components"),
            ("UI/UX Designer", "Design internal helpdesk ticketing portal interfaces"),
            ("Junior Software Developer", "Resolve bugs and escalate software support tickets"),
        ]
        for title, snippet in safe_cases:
            job = self.make_job(title=title, snippet=snippet)
            evaluated = evaluate_job(job)
            self.assertNotEqual(evaluated.get("status"), "Skipped")
            self.assertNotIn("skip_reason", evaluated)
            self.assertGreaterEqual(evaluated.get("suitability_score"), 3)


class TestStaleJobRecoveryOffline(QueueSafetyOfflineBase):
    """Validates recovery of interrupted active jobs, retry counting, and limits."""

    @preserve_project_data_test
    def test_active_job_recovers_to_retry_then_fails_at_limit(self):
        add_job(self.make_job(id="active-job"), self.queue_path)
        mark_job_status("active-job", "Processing", path=self.queue_path)

        loaded = load_queue(self.queue_path)[0]
        base_time = datetime.fromisoformat(loaded["updated_at"])

        # First recovery: retry 1 -> status "Retry"
        recovered = recover_stale_jobs(self.queue_path, max_retries=3, now=base_time + timedelta(seconds=1))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "Retry")
        self.assertEqual(recovered[0]["retry_count"], 1)
        self.assertEqual(recovered[0]["failure_reason"], "WORKER_INTERRUPTED")
        self.assertEqual(recovered[0]["last_interrupted_status"], "Processing")

        # Second recovery: re-enter active, then recover -> retry 2 -> status "Retry"
        mark_job_status("active-job", "Processing", path=self.queue_path)
        recovered = recover_stale_jobs(self.queue_path, max_retries=3, now=base_time + timedelta(seconds=2))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "Retry")
        self.assertEqual(recovered[0]["retry_count"], 2)

        # Third recovery: re-enter active, then reach max_retries (3) -> status "Failed"
        mark_job_status("active-job", "Processing", path=self.queue_path)
        recovered = recover_stale_jobs(self.queue_path, max_retries=3, now=base_time + timedelta(seconds=3))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "Failed")
        self.assertEqual(recovered[0]["retry_count"], 3)
        self.assertEqual(recovered[0]["failure_reason"], "MAX_RETRIES_REACHED")
        self.assertIn("completed_at", recovered[0])

    @preserve_project_data_test
    def test_non_active_jobs_are_not_recovered(self):
        for status in ("Queued", "WaitingForHuman", "Submitted", "Skipped", "Failed", "Retry"):
            write_queue([], self.queue_path)
            add_job(self.make_job(id=f"job-{status}"), self.queue_path)
            mark_job_status(f"job-{status}", status, path=self.queue_path)

            now = datetime.now(timezone.utc) + timedelta(hours=10)
            recovered = recover_stale_jobs(self.queue_path, max_retries=3, now=now)
            self.assertEqual(recovered, [], f"Status {status} should not be recovered as stale")

    @preserve_project_data_test
    def test_fresh_active_jobs_within_threshold_are_not_recovered(self):
        add_job(self.make_job(id="fresh-active"), self.queue_path)
        mark_job_status("fresh-active", "Processing", path=self.queue_path)

        loaded = load_queue(self.queue_path)[0]
        stamp = datetime.fromisoformat(loaded["updated_at"])

        # Stale threshold 60 seconds; checked at +10 seconds
        now = stamp + timedelta(seconds=10)
        recovered = recover_stale_jobs(self.queue_path, max_retries=3, stale_after_seconds=60, now=now)
        self.assertEqual(recovered, [])
        self.assertEqual(load_queue(self.queue_path)[0]["status"], "Processing")

    @preserve_project_data_test
    def test_malformed_timestamp_recovers_safely(self):
        add_job(self.make_job(id="bad-stamp"), self.queue_path)
        mark_job_status("bad-stamp", "Processing", path=self.queue_path)

        # Corrupt updated_at & started_at
        jobs = load_queue(self.queue_path)
        jobs[0]["updated_at"] = "invalid-date-time-string"
        jobs[0]["started_at"] = None
        write_queue(jobs, self.queue_path)

        recovered = recover_stale_jobs(self.queue_path, max_retries=3, stale_after_seconds=0)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "Retry")


class TestSyncQueueIntegrationOffline(QueueSafetyOfflineBase):
    """Validates sync_jobs deduplication, tracker coordination, and metadata updates."""

    @preserve_project_data_test
    def test_sync_adds_new_job_and_evaluates_suitability(self):
        remote = [self.make_job(id="new-remote-1")]
        summary = sync_jobs(remote, local_path=self.queue_path, tracker_path=self.tracker_path)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["duplicates"], 0)

        saved = load_queue(self.queue_path)[0]
        self.assertEqual(saved["status"], "Queued")
        self.assertIn("suitability_score", saved)
        self.assertIn("suitability_reason", saved)

    @preserve_project_data_test
    def test_sync_skips_previously_applied_tracker_job(self):
        save_application(
            "Test Dynamics",
            "React Developer",
            "Mumbai",
            "https://jobs.example.test/roles/101/?ref=daily_digest",
            "GitHub Alerts",
            status="Submitted",
            queue_id="old-queue-id",
            path=self.tracker_path,
        )

        remote = [self.make_job(id="new-alert-same-job")]
        summary = sync_jobs(remote, local_path=self.queue_path, tracker_path=self.tracker_path)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["added"], 0)

        saved = load_queue(self.queue_path)[0]
        self.assertEqual(saved["status"], "Skipped")
        self.assertEqual(saved["skip_reason"], "ALREADY_APPLIED")

    @preserve_project_data_test
    def test_sync_duplicate_preserves_queue_state_and_updates_metadata(self):
        add_job(self.make_job(id="original-id"), self.queue_path)
        mark_job_status("original-id", "Submitted", path=self.queue_path)

        # Incoming remote job with same URL, different alert id, new snippet
        remote = [
            self.make_job(
                id="different-alert-id",
                snippet="Updated job snippet with JavaScript and React details",
            )
        ]
        summary = sync_jobs(remote, local_path=self.queue_path, tracker_path=self.tracker_path)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["updated"], 1)

        saved = load_queue(self.queue_path)[0]
        self.assertEqual(saved["id"], "original-id")
        self.assertEqual(saved["status"], "Submitted")
        self.assertIn("Updated job snippet", saved["snippet"])
        self.assertIn("suitability_score", saved)

    @preserve_project_data_test
    def test_sync_ignores_invalid_remote_records_safely(self):
        invalid_records = [
            {"title": "Dev"},  # missing URL
            {"url": "https://example.test/job"},  # missing title
        ]
        summary = sync_jobs(invalid_records, local_path=self.queue_path, tracker_path=self.tracker_path)
        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["added"], 0)
        self.assertEqual(load_queue(self.queue_path), [])


class TestQueuePrioritizationOffline(QueueSafetyOfflineBase):
    """Validates score-first queue selection, stable FIFO tie-breaking, and score normalization."""

    @preserve_project_data_test
    def test_normalize_suitability_score_edge_cases(self):
        self.assertEqual(normalize_suitability_score(None), 0.0)
        self.assertEqual(normalize_suitability_score(True), 0.0)
        self.assertEqual(normalize_suitability_score(False), 0.0)
        self.assertEqual(normalize_suitability_score("invalid"), 0.0)
        self.assertEqual(normalize_suitability_score(""), 0.0)
        self.assertEqual(normalize_suitability_score([]), 0.0)
        self.assertEqual(normalize_suitability_score({}), 0.0)
        self.assertEqual(normalize_suitability_score(float("nan")), 0.0)
        self.assertEqual(normalize_suitability_score(float("inf")), 0.0)
        self.assertEqual(normalize_suitability_score(float("-inf")), 0.0)
        self.assertEqual(normalize_suitability_score(14.5), 14.5)
        self.assertEqual(normalize_suitability_score("12"), 12.0)
        self.assertEqual(normalize_suitability_score(-7), -7.0)
        self.assertEqual(normalize_suitability_score(0), 0.0)

    @preserve_project_data_test
    def test_get_next_job_selects_highest_score_first(self):
        jobs = [
            self.make_job(id="job-low", suitability_score=3),
            self.make_job(id="job-high", suitability_score=14),
            self.make_job(id="job-mid", suitability_score=8),
        ]
        write_queue(jobs, self.queue_path)
        next_job = get_next_job(self.queue_path)
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["id"], "job-high")

    @preserve_project_data_test
    def test_get_next_job_stable_fifo_tie_breaking(self):
        jobs = [
            self.make_job(id="job-first", suitability_score=11),
            self.make_job(id="job-second", suitability_score=11),
            self.make_job(id="job-third", suitability_score=11),
        ]
        write_queue(jobs, self.queue_path)
        next_job = get_next_job(self.queue_path)
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["id"], "job-first")

    @preserve_project_data_test
    def test_get_next_job_handles_missing_and_corrupt_scores_safely(self):
        jobs = [
            self.make_job(id="job-none", suitability_score=None),
            self.make_job(id="job-corrupt", suitability_score="corrupted_score"),
            self.make_job(id="job-bool", suitability_score=True),
            self.make_job(id="job-valid", suitability_score=2),
        ]
        write_queue(jobs, self.queue_path)
        next_job = get_next_job(self.queue_path)
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["id"], "job-valid")

    @preserve_project_data_test
    def test_get_next_job_ignores_ineligible_statuses(self):
        jobs = [
            self.make_job(id="job-submitted", status="Submitted", suitability_score=100),
            self.make_job(id="job-skipped", status="Skipped", suitability_score=100),
            self.make_job(id="job-waiting", status="WaitingForHuman", suitability_score=100),
            self.make_job(id="job-failed", status="Failed", suitability_score=100),
            self.make_job(id="job-eligible-retry", status="Retry", suitability_score=5),
            self.make_job(id="job-eligible-queued", status="Queued", suitability_score=8),
        ]
        write_queue(jobs, self.queue_path)
        next_job = get_next_job(self.queue_path)
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["id"], "job-eligible-queued")

    @preserve_project_data_test
    def test_get_next_job_returns_none_when_empty_or_no_eligible_jobs(self):
        write_queue([], self.queue_path)
        self.assertIsNone(get_next_job(self.queue_path))

        ineligible_only = [
            self.make_job(id="j1", status="Submitted", suitability_score=10),
            self.make_job(id="j2", status="Skipped", suitability_score=10),
        ]
        write_queue(ineligible_only, self.queue_path)
        self.assertIsNone(get_next_job(self.queue_path))


if __name__ == "__main__":
    unittest.main()
