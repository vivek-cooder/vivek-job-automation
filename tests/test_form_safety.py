"""Local-only regression tests for positive application-form selection."""

import json
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

from tests.data_safety import preserve_project_data_test
from worker.form_adapter import fill_application_form, identify_application_form, prepare_application
from worker.sync_queue import score_job
from worker.navigation import NavigationPolicy


BASE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def applicant():
    return {"name": "Local Test Applicant", "email": "local@example.invalid",
            "phone": "0000000000", "location": "Mumbai", "resume_path": None}


class FormSafetyFixtureTests(unittest.TestCase):
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
            raise unittest.SkipTest(f"Local Chromium unavailable: {exc}")

    def setUp(self):
        self._navigation_policy_patch = patch(
            "worker.form_adapter.configured_policy",
            return_value=NavigationPolicy.for_local_tests(),
        )
        self._navigation_policy_patch.start()
        self.addCleanup(self._navigation_policy_patch.stop)

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

    @preserve_project_data_test
    def test_vestval_listing_ignores_newsletter_and_follows_clear_application_link(self):
        page = self.open_fixture("vestval_listing.html")
        try:
            page.add_init_script("""() => document.addEventListener('input', event => {
              if (event.target && event.target.name === 'newsletter_email') window.name = 'newsletter-filled';
            })""")
            self.assertEqual(identify_application_form(page)["status"], "Unsupported")
            result = prepare_application(page, applicant(), auto_submit=False)
            self.assertEqual(result["status"], "ReadyForSubmission")
            self.assertEqual(result["original_url"], (FIXTURES / "vestval_listing.html").as_uri())
            self.assertEqual(result["application_url"], (FIXTURES / "vestval_application.html").as_uri())
            self.assertNotEqual(page.evaluate("() => window.name"), "newsletter-filled")
            self.assertEqual(page.locator("[name=applicant_email]").input_value(), applicant()["email"])
            self.assertEqual(page.locator("[name=full_name]").input_value(), applicant()["name"])
            self.assertEqual(page.locator("[name=phone]").input_value(), applicant()["phone"])
        finally:
            page.close()

    @preserve_project_data_test
    def test_unrelated_forms_are_rejected_and_only_job_application_is_filled(self):
        page = self.open_fixture("multi_form_application.html")
        try:
            selection = identify_application_form(page)
            self.assertEqual(selection["status"], "Selected")
            self.assertEqual(selection["form"].get_attribute("id"), "jobApplication")
            result = prepare_application(page, applicant(), auto_submit=False)
            self.assertEqual(result["status"], "ReadyForSubmission")
            for selector in ("[name=newsletter_email]", "[name=contact_name]", "[name=contact_email]",
                             "[name=contact_phone]", "[name=q]"):
                self.assertEqual(page.locator(selector).input_value(), "", selector)
            self.assertEqual(page.locator("[name=applicant_email]").input_value(), applicant()["email"])
        finally:
            page.close()

    @preserve_project_data_test
    def test_two_plausible_application_forms_stop_without_filling(self):
        page = self.open_fixture("ambiguous_applications.html")
        try:
            result = prepare_application(page, applicant(), auto_submit=False)
            self.assertEqual(result["status"], "NeedsUserInput")
            self.assertEqual(result["reason"], "AMBIGUOUS_APPLICATION_FORM")
            for selector in ("[name=name_a]", "[name=email_a]", "[name=phone_a]",
                             "[name=name_b]", "[name=email_b]", "[name=phone_b]",
                             "[name=newsletter_email]"):
                self.assertEqual(page.locator(selector).input_value(), "", selector)
        finally:
            page.close()

    @preserve_project_data_test
    def test_listing_without_form_or_clear_link_is_unsupported(self):
        page = self.open_fixture("no_application_listing.html")
        try:
            result = prepare_application(page, applicant(), auto_submit=False)
            self.assertEqual(result["status"], "Failed")
            self.assertEqual(result["failure_reason"], "UNSUPPORTED_APPLICATION_FLOW")
            self.assertEqual(page.locator("[name=email]").input_value(), "")
        finally:
            page.close()

    @preserve_project_data_test
    def test_sensitive_and_unknown_fields_are_left_untouched(self):
        page = self.open_fixture("sensitive_application.html")
        try:
            report = fill_application_form(page, applicant())
            self.assertTrue(report["field_count"])
            self.assertEqual(page.locator("[name=full_name]").input_value(), applicant()["name"])
            self.assertEqual(page.locator("[name=email]").input_value(), applicant()["email"])
            self.assertEqual(page.locator("[name=phone]").input_value(), applicant()["phone"])
            for selector in ("[name=expected_salary]", "[name=work_authorization]", "[name=unknown_question]"):
                self.assertEqual(page.locator(selector).input_value(), "", selector)
            self.assertTrue({"salary", "work_authorization", "unknown"}.issubset(report["needs_user_input"]))
            self.assertFalse(json.loads((BASE / "config.json").read_text(encoding="utf-8"))["auto_submit"])
        finally:
            page.close()

    @preserve_project_data_test
    def test_synthetic_novami_mapping_matching_position_and_optional_fields(self):
        page = self.open_fixture("synthetic_novami_application.html")
        try:
            synthetic_profile = {
                "name": "Test Applicant",
                "email": "test@example.invalid",
                "phone": "0000000000",
                "position": "Frontend Developer (Engineering)",
                "portfolio": "https://portfolio.example.invalid",
                "linkedin": "https://linkedin.example.invalid/in/test",
                "cover_letter": "Synthetic cover letter content.",
            }
            # Fill custom required field manually so remaining mapping is evaluated
            page.locator("#unknownCustom").fill("prefilled-custom-code")

            report = fill_application_form(page, synthetic_profile)
            self.assertEqual(page.locator("#fullName").input_value(), "Test Applicant")
            self.assertEqual(page.locator("#email").input_value(), "test@example.invalid")
            self.assertEqual(page.locator("#phone").input_value(), "0000000000")
            self.assertEqual(page.locator("#position").input_value(), "Frontend Developer (Engineering)")
            self.assertEqual(page.locator("#portfolioUrl").input_value(), "https://portfolio.example.invalid")
            self.assertEqual(page.locator("#linkedinUrl").input_value(), "https://linkedin.example.invalid/in/test")
            self.assertEqual(page.locator("#coverLetter").input_value(), "Synthetic cover letter content.")
            self.assertEqual(page.locator("#unrelatedOptional").input_value(), "")
            self.assertEqual(report["needs_user_input"], [])
            self.assertTrue(report["valid_for_submission"])
        finally:
            page.close()

    @preserve_project_data_test
    def test_synthetic_novami_position_mismatch_halts_safely_without_selecting_unrelated(self):
        page = self.open_fixture("synthetic_novami_application.html")
        try:
            mismatched_profile = {
                "name": "Test Applicant",
                "email": "test@example.invalid",
                "phone": "0000000000",
                "position": "Data Scientist",
            }
            report = fill_application_form(page, mismatched_profile)
            # Position dropdown must not guess or select unrelated options (e.g. Digital Marketing or Graphic Designer)
            self.assertEqual(page.locator("#position").input_value(), "")
            self.assertIn("position", report["needs_user_input"])
            self.assertFalse(report["valid_for_submission"])
        finally:
            page.close()

    @preserve_project_data_test
    def test_synthetic_novami_missing_optional_fields_left_blank(self):
        page = self.open_fixture("synthetic_novami_application.html")
        try:
            minimal_profile = {
                "name": "Test Applicant",
                "email": "test@example.invalid",
                "phone": "0000000000",
                "position": "Frontend Developer (Engineering)",
                "portfolio": None,
                "linkedin": None,
                "cover_letter": None,
            }
            report = fill_application_form(page, minimal_profile)
            self.assertEqual(page.locator("#fullName").input_value(), "Test Applicant")
            self.assertEqual(page.locator("#portfolioUrl").input_value(), "")
            self.assertEqual(page.locator("#linkedinUrl").input_value(), "")
            self.assertEqual(page.locator("#coverLetter").input_value(), "")
            # Optional fields with missing values must not trigger needs_user_input
            self.assertNotIn("portfolio", report["needs_user_input"])
            self.assertNotIn("linkedin", report["needs_user_input"])
            self.assertNotIn("cover_letter", report["needs_user_input"])
        finally:
            page.close()

    @preserve_project_data_test
    def test_synthetic_novami_unknown_required_field_halts_safely(self):
        page = self.open_fixture("synthetic_novami_application.html")
        try:
            synthetic_profile = {
                "name": "Test Applicant",
                "email": "test@example.invalid",
                "phone": "0000000000",
                "position": "Frontend Developer (Engineering)",
            }
            report = fill_application_form(page, synthetic_profile)
            self.assertEqual(page.locator("#unknownCustom").input_value(), "")
            self.assertIn("unknown", report["needs_user_input"])
            self.assertFalse(report["valid_for_submission"])
        finally:
            page.close()

    @preserve_project_data_test
    def test_synthetic_novami_form_never_submits_when_auto_submit_disabled(self):
        page = self.open_fixture("synthetic_novami_application.html")
        try:
            synthetic_profile = {
                "name": "Test Applicant",
                "email": "test@example.invalid",
                "phone": "0000000000",
                "position": "Frontend Developer (Engineering)",
                "portfolio": "https://portfolio.example.invalid",
            }
            # Pre-fill custom question so form is otherwise complete
            page.locator("#unknownCustom").fill("valid-token")
            result = prepare_application(page, synthetic_profile, auto_submit=False)
            self.assertEqual(result["status"], "ReadyForSubmission")
            self.assertFalse(result.get("submission_enabled", False))
            self.assertIsNone(result.get("submission_blocked_reason"))
        finally:
            page.close()


class FormSafetyUnitTests(unittest.TestCase):
    def test_ui_ux_punctuation_variants_are_equivalent_and_specific(self):
        variants = ("UI/UX Designer", "UI UX Designer", "UI / UX Designer",
                    "UI-UX Designer", "UI – UX Designer", "UI — UX Designer")
        scores = [score_job({"title": title})[0] for title in variants]
        self.assertEqual(scores, [5] * len(variants))
        unrelated = [score_job({"title": title})[0]
                     for title in ("UX Researcher", "Graphic Designer", "Product Manager", "Accountant")]
        self.assertTrue(all(score < 5 for score in unrelated), unrelated)

    def test_worker_help_exits_without_starting_playwright(self):
        result = subprocess.run(
            [sys.executable, "-m", "worker.runner", "--help"], cwd=BASE,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.casefold())
        self.assertNotIn("Browser ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
