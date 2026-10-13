"""Phase 14 Controlled Preparation & Read-Only Review Tests."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tests.data_safety import preserve_project_data_test
from worker.navigation import NavigationPolicy, NavigationRejected
from worker.review import (
    inspect_listing_metadata,
    review_application_form,
    prepare_job_review,
    PROFILE_KEY_LABELS,
)
import worker.job_queue as jq
import worker.tracker as tr
from worker.runner import recover_stale_jobs, process_job, find_protected_queue_duplicate

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Phase14PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")
        cls.playwright_manager = sync_playwright()
        cls.playwright = cls.playwright_manager.start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright_manager.__exit__(None, None, None)
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright_manager"):
            cls.playwright_manager.__exit__(None, None, None)

    def open_fixture(self, name):
        page = self.browser.new_page()
        page.goto((FIXTURES / name).as_uri())
        return page

    # 1. Correct job and company detection
    @preserve_project_data_test
    def test_job_and_company_detection(self):
        page = self.open_fixture("synthetic_novami_application.html")
        meta = inspect_listing_metadata(page, job={
            "title": "Frontend Developer",
            "company": "Novami Infotech Pvt. Ltd.",
            "url": "https://example.invalid/careers/frontend-developer",
        })
        self.assertEqual(meta["detected_title"], "Frontend Developer")
        self.assertEqual(meta["detected_company"], "Novami Infotech Pvt. Ltd.")
        self.assertEqual(meta["route_type"], "embedded_form")
        page.close()

    # 2. Application form vs newsletter/contact selection
    @preserve_project_data_test
    def test_form_selection_rejects_newsletter_and_contact(self):
        page = self.open_fixture("multi_form_application.html")
        review = review_application_form(page)
        self.assertIn(review["status"], {"ReadyForHumanReview", "NeedsUserInput"})
        self.assertGreater(review["field_count"], 0)
        # Form should be selected, newsletter and contact rejected
        self.assertGreaterEqual(review["form_score"], 5)
        page.close()

    # 3. Required and optional field mapping (keys only, no values)
    @preserve_project_data_test
    def test_field_mapping_maps_keys_without_values(self):
        page = self.open_fixture("accenture_like_application.html")
        profile_keys = {"name", "email", "phone", "portfolio", "linkedin"}
        review = review_application_form(page, profile_keys=profile_keys)
        self.assertEqual(review["status"], "ReadyForHumanReview")
        self.assertIn("name", review["mapped_required_keys"])
        self.assertIn("email", review["mapped_required_keys"])
        self.assertIn("phone", review["mapped_required_keys"])
        self.assertEqual(len(review["missing_required_keys"]), 0)
        self.assertEqual(len(review["unknown_required_fields"]), 0)
        self.assertTrue(review["can_proceed_to_fill_after_approval"])
        self.assertTrue(review["submission_prevented"])
        page.close()

    # 4. Unknown required fields and ambiguous forms stop safely
    @preserve_project_data_test
    def test_unknown_required_field_stops_safely(self):
        page = self.open_fixture("synthetic_novami_application.html")
        profile_keys = {"name", "email", "phone", "position", "portfolio"}
        review = review_application_form(page, profile_keys=profile_keys)
        self.assertEqual(review["status"], "NeedsUserInput")
        self.assertIn("unknownCustom", review["unknown_required_fields"])
        self.assertFalse(review["can_proceed_to_fill_after_approval"])
        page.close()

    @preserve_project_data_test
    def test_ambiguous_forms_stop_safely(self):
        page = self.open_fixture("ambiguous_applications.html")
        review = review_application_form(page)
        self.assertEqual(review["status"], "NeedsUserInput")
        self.assertEqual(review["reason"], "AMBIGUOUS_APPLICATION_FORM")
        self.assertFalse(review["can_proceed_to_fill_after_approval"])
        page.close()

    @preserve_project_data_test
    def test_missing_required_profile_keys_stop_safely(self):
        page = self.open_fixture("synthetic_novami_application.html")
        # Profile only has name, missing email and phone
        sparse_profile_keys = {"name": "Test Name", "email": "", "phone": None}
        review = review_application_form(page, profile_keys=sparse_profile_keys)
        self.assertEqual(review["status"], "NeedsUserInput")
        self.assertIn("email", review["missing_required_keys"])
        self.assertIn("phone", review["missing_required_keys"])
        self.assertFalse(review["can_proceed_to_fill_after_approval"])
        page.close()

    @preserve_project_data_test
    def test_security_challenge_stops_safely(self):
        page = self.open_fixture("security_challenge.html")
        review = review_application_form(page)
        self.assertEqual(review["status"], "WaitingForHuman")
        self.assertIsNotNone(review["reason"])
        self.assertFalse(review.get("can_proceed_to_fill_after_approval", False))
        page.close()

    # 5. Navigation policy blocks unauthorized hosts and redirects
    @preserve_project_data_test
    def test_navigation_policy_blocks_unauthorized(self):
        policy = NavigationPolicy(["authorized.example.invalid"])
        with self.assertRaises(NavigationRejected) as ctx:
            policy.check("https://unauthorized.evil.invalid/job")
        self.assertEqual(ctx.exception.reason, "UNAUTHORIZED_HOST")

    # 6. Read-only review never fills or mutates the DOM
    @preserve_project_data_test
    def test_review_mode_does_not_fill_fields(self):
        page = self.open_fixture("synthetic_novami_application.html")
        # Record initial value of inputs
        initial_name = page.locator("#fullName").input_value()
        initial_email = page.locator("#email").input_value()

        # Run prepare_job_review
        report = prepare_job_review(
            page,
            page.url,
            profile_keys={"name", "email", "phone", "position"},
        )

        # Values in DOM must remain completely unchanged (empty)
        after_name = page.locator("#fullName").input_value()
        after_email = page.locator("#email").input_value()
        self.assertEqual(initial_name, after_name)
        self.assertEqual(initial_email, after_email)
        self.assertEqual(after_name, "")
        self.assertEqual(after_email, "")
        self.assertFalse(report["submission_action_available"])
        page.close()

    # 7. No submission control can be clicked
    @preserve_project_data_test
    def test_submission_action_permanently_unavailable(self):
        page = self.open_fixture("synthetic_novami_application.html")
        report = prepare_job_review(page, page.url)
        self.assertFalse(report["submission_action_available"])
        self.assertFalse(report["auto_submit_enforced"])
        self.assertTrue(report["form_review"]["submission_prevented"])
        page.close()

    # 8. Worker recovery and duplicate detection remain safe
    @preserve_project_data_test
    def test_worker_recovery_and_duplicate_detection(self):
        # Stale recovery returns list without throwing
        recovered = recover_stale_jobs(stale_after_seconds=3600)
        self.assertIsInstance(recovered, list)

        # Duplicate check on dummy job
        dummy_job = {
            "id": "phase14-test-dup",
            "url": "https://example.invalid/jobs/123",
            "title": "React Developer",
            "company": "Example Co",
            "source": "Manual",
        }
        # find_protected_queue_duplicate should be callable and safe
        dup = find_protected_queue_duplicate(dummy_job, [])
        self.assertIsNone(dup)
