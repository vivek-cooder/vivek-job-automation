import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worker.job_queue import (
    add_job, find_protected_queue_duplicate, get_job_key, load_queue,
    mark_job_status, recover_stale_jobs,
)
from worker.sync_queue import evaluate_job, score_job, sync_jobs
from worker.tracker import save_application
from tests.data_safety import preserve_project_data_test


BASE = Path(__file__).resolve().parents[1]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = self.root / "application_queue.json"
        self.tracker = self.root / "applications.json"

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **overrides):
        value = {
            "id": "alert-001", "title": "React Developer",
            "company": "Example Co", "source": "GitHub Alerts",
            "url": "https://jobs.example.com/role/42/?job=42",
            "location": "Mumbai", "status": "Queued",
        }
        value.update(overrides)
        return value

    def test_new_job_is_queued_and_duplicate_insert_is_idempotent(self):
        added, record, error = add_job(self.job(), self.queue)
        self.assertTrue(added)
        self.assertEqual(record["status"], "Queued")
        again, _, error = add_job(self.job(), self.queue)
        self.assertFalse(again)
        self.assertEqual(error, "DUPLICATE_JOB")
        self.assertEqual(len(load_queue(self.queue)), 1)

    def test_same_url_with_changed_queue_id_is_duplicate(self):
        add_job(self.job(), self.queue)
        added, _, reason = add_job(self.job(id="different-alert-id"), self.queue)
        self.assertFalse(added)
        self.assertEqual(reason, "DUPLICATE_JOB")
        self.assertEqual(len(load_queue(self.queue)), 1)

    def test_same_company_title_with_distinct_job_urls_is_not_duplicate(self):
        add_job(self.job(url="https://jobs.example.com/role/one"), self.queue)
        added, _, reason = add_job(
            self.job(id="alert-002", url="https://jobs.example.com/role/two"),
            self.queue,
        )
        self.assertTrue(added, reason)
        self.assertEqual(len(load_queue(self.queue)), 2)

    def test_active_or_submitted_queue_duplicate_is_protected(self):
        active = self.job(id="active-id", status="WaitingForHuman")
        incoming = self.job(id="new-id", status="Queued")
        self.assertEqual(find_protected_queue_duplicate(incoming, [active]), active)
        submitted = dict(active, status="Submitted")
        self.assertEqual(find_protected_queue_duplicate(incoming, [submitted]), submitted)
        failed = dict(active, status="Failed")
        self.assertIsNone(find_protected_queue_duplicate(incoming, [failed]))

    def test_source_company_title_fallback_and_url_canonicalization(self):
        self.assertEqual(
            get_job_key(self.job(url="https://JOBS.example.com/role/42/?job=42#section")),
            get_job_key(self.job(url="https://jobs.example.com/role/42?job=42")),
        )
        first = self.job(id=None, url="", source="Board", company="ABC", title="UI Designer")
        second = dict(first, title="  ui   designer ")
        self.assertEqual(get_job_key(first), get_job_key(second))

    def test_sync_marks_previously_submitted_job_skipped(self):
        save_application(
            "Example Co", "React Developer", "Mumbai",
            "https://jobs.example.com/role/42/?job=42", "GitHub Alerts",
            status="Submitted", queue_id="old-id", path=self.tracker,
        )
        summary = sync_jobs([self.job(id="new-id")], local_path=self.queue, tracker_path=self.tracker)
        self.assertEqual(summary["skipped"], 1)
        saved = load_queue(self.queue)[0]
        self.assertEqual(saved["status"], "Skipped")
        self.assertEqual(saved["skip_reason"], "ALREADY_APPLIED")

    def test_duplicate_sync_preserves_submitted_state_and_updates_metadata(self):
        add_job(self.job(), self.queue)
        mark_job_status("alert-001", "Submitted", path=self.queue)
        summary = sync_jobs([self.job(id="replacement-alert-id", snippet="React and JavaScript")], local_path=self.queue, tracker_path=self.tracker)
        saved = load_queue(self.queue)[0]
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(saved["status"], "Submitted")
        self.assertEqual(saved["id"], "alert-001")
        self.assertIn("suitability_score", saved)

    def test_stale_active_job_recovers_then_fails_at_retry_limit(self):
        add_job(self.job(), self.queue)
        mark_job_status("alert-001", "Processing", path=self.queue)
        from worker.job_queue import load_queue
        stamp = datetime.fromisoformat(load_queue(self.queue)[0]["updated_at"])
        now = stamp + timedelta(seconds=1)
        recovered = recover_stale_jobs(self.queue, max_retries=3, now=now)
        self.assertEqual(recovered[0]["status"], "Retry")
        self.assertEqual(recovered[0]["retry_count"], 1)
        mark_job_status("alert-001", "Processing", path=self.queue)
        recovered = recover_stale_jobs(self.queue, max_retries=3, now=now + timedelta(seconds=1))
        self.assertEqual(recovered[0]["status"], "Retry")
        mark_job_status("alert-001", "Processing", path=self.queue)
        recovered = recover_stale_jobs(self.queue, max_retries=3, now=now + timedelta(seconds=2))
        self.assertEqual(recovered[0]["status"], "Failed")
        self.assertEqual(recovered[0]["failure_reason"], "MAX_RETRIES_REACHED")

    def test_suitability_scores_target_role_and_skips_unrelated(self):
        react = self.job(snippet="JavaScript, entry level, 0-1 years")
        score, reason = score_job(react)
        self.assertGreaterEqual(score, 10)
        self.assertTrue(reason)
        unrelated = evaluate_job(self.job(title="Accountant", snippet="Entry level"))
        self.assertEqual(unrelated["status"], "Skipped")
        self.assertEqual(unrelated["skip_reason"], "UNRELATED_ROLE")
        borderline = evaluate_job(self.job(title="Product Specialist"))
        self.assertNotEqual(borderline.get("status"), "Skipped")


class LocalFixtureTests(unittest.TestCase):
    @preserve_project_data_test
    def test_existing_application_fixture_flow(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed in this interpreter")
        fixture = BASE / "test_application.html"
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Local Chromium unavailable: {exc}")
            page = browser.new_page()
            try:
                page.goto(fixture.as_uri())
                page.locator("#name").fill("Test Applicant")
                page.locator("#email").fill("test@example.invalid")
                page.locator("#phone").fill("0000000000")
                page.locator("#location").fill("Mumbai")
                page.locator("#experience").fill("Entry level")
                page.locator("#verificationCode").fill("123456")
                page.locator("#verifyButton").click()
                self.assertTrue(page.locator("#submitButton").is_enabled())
                page.locator("#submitButton").click()
                page.locator("#submitSuccess").wait_for(state="visible")
                self.assertTrue(page.locator("#submitSuccess").is_visible())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
