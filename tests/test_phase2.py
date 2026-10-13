"""Unit coverage for Phase 2 form logic using local, in-memory mock pages."""

import json
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.applicant import load_applicant_profile
from worker.detector import detect_application_result, detect_verification
from worker.form_adapter import analyze_form, fill_application_form, prepare_application
from worker.settings import auto_submit_enabled
from worker.navigation import NavigationPolicy


_TEST_NAVIGATION_POLICY = NavigationPolicy.for_local_tests(
    allowed_hosts=("127.0.0.1", "localhost", "::1", "local.invalid", "employer.example"),
)
_FORM_POLICY_PATCH = None


def setUpModule():
    global _FORM_POLICY_PATCH
    _FORM_POLICY_PATCH = patch("worker.form_adapter.configured_policy",
                               return_value=_TEST_NAVIGATION_POLICY)
    _FORM_POLICY_PATCH.start()


def tearDownModule():
    if _FORM_POLICY_PATCH:
        _FORM_POLICY_PATCH.stop()


class FakeField:
    def __init__(self, *, label="", name="", field_id="", placeholder="", tag="input",
                 input_type="text", value="", required=False, checked=False,
                 group="", options=None):
        self.data = {
            "tag": tag, "type": input_type, "name": name, "id": field_id,
            "placeholder": placeholder, "aria_label": "", "label": label,
            "required": required, "value": value, "checked": checked,
            "group": group, "options": options or [],
        }

    def evaluate(self, _script):
        return self.data

    def is_visible(self):
        return True

    def fill(self, value):
        self.data["value"] = value

    def check(self):
        self.data["checked"] = True
        self.data["value"] = "on"

    def select_option(self, value):
        self.data["value"] = value

    def set_input_files(self, value):
        self.data["value"] = Path(value).name


class FakeButton:
    def __init__(self, page, text="Submit Application"):
        self.page, self.text = page, text
        self.clicks = 0

    def inner_text(self):
        return self.text

    def get_attribute(self, _name):
        return self.text

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.clicks += 1
        if self.page.submit_outcome == "success":
            self.page.success = True
        elif self.page.submit_outcome == "failure":
            self.page.body = "Unable to submit your application. Please try again."


class FakeLocator:
    def __init__(self, items):
        self.items = items

    @property
    def first(self):
        return self.items[0] if self.items else self

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    def inner_text(self, **_kwargs):
        return self.items[0] if self.items else ""

    def is_visible(self):
        if not self.items:
            return False
        return getattr(self.items[0], "is_visible", lambda: True)()

    def is_enabled(self):
        return bool(self.items) and getattr(self.items[0], "is_enabled", lambda: True)()


class FakeForm:
    def __init__(self, page):
        self.page = page

    def evaluate(self, _script):
        return {"action": "", "id": "jobApplication", "name": "",
                "class_name": "", "text": "Job Application", "heading": "",
                "headings": ["Job Application"], "buttons": ["Submit Application"]}

    def locator(self, selector):
        if selector == "input, textarea, select":
            return FakeLocator(self.page.fields)
        if selector == "button":
            return FakeLocator(self.page.buttons)
        if selector == "input[type='submit']":
            return FakeLocator([])
        return FakeLocator([])


class FakePage:
    def __init__(self, fields=(), body="", *, submit_outcome="success"):
        self.fields = list(fields)
        self.body = body
        self.url = "file:///local/test_application.html"
        self.submit_outcome = submit_outcome
        self.success = False
        self.gate_markers = set()
        self.buttons = [FakeButton(self)]
        self.form = FakeForm(self)
        self.closed = False

    def locator(self, selector):
        if selector == "form":
            return FakeLocator([self.form])
        if selector == "input, textarea, select":
            return FakeLocator(self.fields)
        if selector == "body":
            return FakeLocator([self.body])
        if selector == "button":
            return FakeLocator(self.buttons)
        if selector == "input[type='submit']":
            return FakeLocator([])
        if selector == "#submitSuccess":
            # Match the Playwright Locator surface consumed by detector._visible.
            return FakeLocator([FakeField()]) if self.success else FakeLocator([])
        if selector in self.gate_markers:
            return FakeLocator([object()])
        return FakeLocator([])

    def evaluate(self, _script):
        return True

    def wait_for_timeout(self, _ms):
        return None

    def is_closed(self):
        return self.closed


def filled_profile(**extra):
    values = {
        "name": "Test Applicant", "email": "test@example.invalid",
        "phone": "0000000000", "location": "Mumbai",
        "experience": "Frontend Developer / UI/UX Designer",
        "resume_path": None,
    }
    values.update(extra)
    return values


class FormAnalysisTests(unittest.TestCase):
    def test_full_name_email_phone_detected_and_filled(self):
        fields = [
            FakeField(label="Full name", name="candidate_name", required=True),
            FakeField(label="Email address", input_type="email", required=True),
            FakeField(label="Phone number", input_type="tel"),
        ]
        report = fill_application_form(FakePage(fields), filled_profile())
        self.assertEqual([x["field_type"] for x in report["fields"]], ["full_name", "email", "phone"])
        self.assertEqual([x["status"] for x in report["fields"]], ["filled", "filled", "filled"])
        self.assertTrue(report["valid_for_submission"])

    def test_resume_linkedin_github_and_portfolio_detection(self):
        fields = [
            FakeField(label="Upload resume", input_type="file", required=True),
            FakeField(label="LinkedIn profile URL"),
            FakeField(label="GitHub URL"),
            FakeField(label="Portfolio website"),
        ]
        report = fill_application_form(FakePage(fields), filled_profile())
        self.assertEqual([x["field_type"] for x in report["fields"]], ["resume", "linkedin", "github", "portfolio"])
        self.assertEqual(report["fields"][0]["status"], "needs_user_input")
        self.assertEqual(report["fields"][1]["status"], "optional_empty")

    def test_first_last_experience_skills_and_cover_letter_classification(self):
        fields = [
            FakeField(label="First name", name="given_name", required=True),
            FakeField(label="Last name", name="family_name", required=True),
            FakeField(label="Years of experience", name="experience"),
            FakeField(label="Technical skills", name="skills"),
            FakeField(label="Cover letter", name="cover_letter", tag="textarea"),
        ]
        applicant = filled_profile(
            name="Test Applicant", experience="1 year", skills="React, JavaScript",
            cover_letter="Local test cover letter",
        )
        report = fill_application_form(FakePage(fields), applicant)
        self.assertEqual(
            [item["field_type"] for item in report["fields"]],
            ["first_name", "last_name", "experience", "skills", "cover_letter"],
        )
        self.assertEqual([field.data["value"] for field in fields], [
            "Test", "Applicant", "1 year", "React, JavaScript", "Local test cover letter",
        ])
        self.assertTrue(report["valid_for_submission"])

    def test_configured_resume_is_uploaded_only_when_file_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            resume = Path(temp) / "resume.pdf"
            resume.write_bytes(b"local test resume")
            field = FakeField(label="Resume", input_type="file", required=True)
            report = fill_application_form(FakePage([field]), filled_profile(resume_path=str(resume)))
            self.assertEqual(report["fields"][0]["status"], "filled")

    def test_missing_resume_configuration_requires_user_input(self):
        field = FakeField(label="Upload resume", input_type="file", required=True)
        report = fill_application_form(FakePage([field]), filled_profile(resume_path=None))
        self.assertEqual(report["fields"][0]["status"], "needs_user_input")
        self.assertEqual(report["missing_fields"], ["resume"])

    def test_unknown_optional_field_is_not_guessed_or_blocking(self):
        field = FakeField(label="Something else", name="other_answer")
        report = fill_application_form(FakePage([field]), filled_profile())
        self.assertEqual(report["fields"][0]["field_type"], "unknown")
        self.assertEqual(report["fields"][0]["status"], "optional_empty")
        self.assertTrue(report["valid_for_submission"])

    def test_select_requires_exact_profile_option_match(self):
        field = FakeField(label="Location", tag="select", input_type="select-one", required=True,
                          options=[{"value": "Mumbai", "label": "Mumbai"}, {"value": "Remote", "label": "Remote"}])
        report = fill_application_form(FakePage([field]), filled_profile(location="Mumbai"))
        self.assertEqual(report["fields"][0]["status"], "filled")
        self.assertEqual(field.data["value"], "Mumbai")

    def test_checkbox_radio_and_preselected_values_are_safe(self):
        checkbox = FakeField(label="Work authorization", name="work_authorization", input_type="checkbox", required=True)
        checked = FakeField(label="Email", value="existing@example.invalid")
        radio_yes = FakeField(label="Yes", name="work_authorization", input_type="radio", group="auth", value="yes", required=True)
        radio_no = FakeField(label="No", name="work_authorization", input_type="radio", group="auth", value="no", required=True)
        report = fill_application_form(FakePage([checkbox, checked, radio_yes, radio_no]), filled_profile(work_authorization=None))
        self.assertEqual(report["fields"][0]["status"], "needs_user_input")
        self.assertEqual(report["fields"][1]["status"], "already_filled")
        self.assertFalse(checkbox.data["checked"])

        yes = FakeField(label="Yes", name="work_authorization", input_type="radio", group="auth", value="yes", required=True)
        no = FakeField(label="No", name="work_authorization", input_type="radio", group="auth", value="no", required=True)
        report = fill_application_form(FakePage([yes, no]), filled_profile(work_authorization="yes"))
        self.assertEqual(report["fields"][0]["status"], "filled")
        self.assertTrue(yes.data["checked"])

    def test_unknown_required_field_needs_user_input(self):
        field = FakeField(label="Tell us something else", name="extra_answer", required=True)
        report = fill_application_form(FakePage([field]), filled_profile())
        self.assertEqual(report["fields"][0]["field_type"], "unknown")
        self.assertEqual(report["fields"][0]["status"], "needs_user_input")
        self.assertIn("unknown", report["missing_fields"])

    def test_sensitive_fields_are_never_guessed(self):
        fields = [
            FakeField(label="Are you authorized to work here?", required=True),
            FakeField(label="Will you require visa sponsorship?", required=True),
            FakeField(label="I certify this information is accurate", input_type="checkbox", required=True),
            FakeField(label="Desired salary", required=True),
            FakeField(label="Notice period", required=True),
            FakeField(label="Education", required=True),
        ]
        report = fill_application_form(FakePage(fields), filled_profile())
        self.assertEqual(report["missing_fields"], ["education", "notice_period", "salary", "sponsorship", "unknown", "work_authorization"])
        self.assertTrue(all(x["status"] == "needs_user_input" for x in report["fields"]))

    def test_textarea_is_filled_from_explicit_profile_value(self):
        field = FakeField(label="Cover letter", tag="textarea", required=True)
        report = fill_application_form(FakePage([field]), filled_profile(cover_letter="Specific test text"))
        self.assertEqual(field.data["value"], "Specific test text")
        self.assertEqual(report["fields"][0]["status"], "filled")

    def test_form_interaction_exception_becomes_needs_user_input(self):
        field = FakeField(label="Full name", required=True)
        def broken_fill(_value):
            raise RuntimeError("local mock interaction failure")
        field.fill = broken_fill
        report = fill_application_form(FakePage([field]), filled_profile())
        self.assertEqual(report["fields"][0]["status"], "needs_user_input")
        self.assertEqual(report["missing_fields"], ["full_name"])
        self.assertIn("RuntimeError", report["fields"][0]["reason"])


class HumanGateTests(unittest.TestCase):
    def test_captcha_otp_email_phone_and_cloudflare_detection(self):
        self.assertEqual(detect_verification(FakePage(body="Please verify you are human")), "CAPTCHA_REQUIRED")
        self.assertEqual(detect_verification(FakePage(body="reCAPTCHA challenge")), "CAPTCHA_REQUIRED")
        self.assertEqual(detect_verification(FakePage(body="hCaptcha security check")), "CAPTCHA_REQUIRED")
        self.assertEqual(detect_verification(FakePage(body="Enter the one-time password")), "OTP_REQUIRED")
        self.assertEqual(detect_verification(FakePage(body="Please verify your email")), "EMAIL_VERIFICATION_REQUIRED")
        self.assertEqual(detect_verification(FakePage(body="Please verify your phone")), "PHONE_VERIFICATION_REQUIRED")
        page = FakePage(body="Checking your browser before accessing the site")
        self.assertEqual(detect_verification(page), "SECURITY_CHALLENGE_REQUIRED")

    def test_human_gate_pauses_and_resumes_after_user_action(self):
        field = FakeField(label="Email", input_type="email", required=True)
        page = FakePage([field], body="Verify you are human")
        events = []

        def pause(status, reason, details, url):
            events.append((status, reason, url))
            page.body = "Application form is ready"

        def resume(url):
            events.append(("RESUMED", url))

        result = prepare_application(page, filled_profile(), on_wait=pause, on_resume=resume)
        self.assertEqual(result["status"], "ReadyForSubmission")
        self.assertEqual(events[0][0], "WaitingForHuman")
        self.assertEqual(events[1][0], "RESUMED")
        self.assertFalse(page.closed)

    def test_wait_callbacks_preserve_reason_url_and_job_identity(self):
        runner = load_runner_module()
        job = {"id": "local-queue-7", "url": "https://local.invalid/apply", "title": "Test Role"}
        tracker_job = {"company": "Local Co", "job_title": "Test Role", "url": job["url"]}
        with patch.object(runner, "mark_job_status") as mark, \
             patch.object(runner, "update_application_status") as update:
            on_wait, on_resume = runner._make_wait_callbacks(job, "url:local-test", tracker_job)
            on_wait("WaitingForHuman", "OTP_REQUIRED", {}, job["url"])
            self.assertEqual(mark.call_args_list[0].args[:2], ("local-queue-7", "WaitingForHuman"))
            metadata = mark.call_args_list[0].kwargs["metadata"]
            self.assertEqual(metadata["waiting_reason"], "OTP_REQUIRED")
            self.assertEqual(metadata["current_url"], job["url"])
            self.assertEqual(metadata["queue_id"], "local-queue-7")
            self.assertEqual(metadata["job_key"], "url:local-test")
            self.assertEqual(update.call_args_list[0].args[1], "WaitingForHuman")
            on_resume(job["url"])
            self.assertEqual(mark.call_args.args[:2], ("local-queue-7", "Processing"))
            self.assertEqual(update.call_args.args[1], "Processing")

    def test_required_user_input_pauses_then_rechecks_without_overwrite(self):
        salary = FakeField(label="Desired salary", required=True)
        page = FakePage([salary], body="Application form")
        waits = []

        def pause(status, reason, details, url):
            waits.append((status, details["missing_fields"]))
            salary.fill("85000")  # Simulated manual entry in a local mock page.

        result = prepare_application(page, filled_profile(), on_wait=pause)
        self.assertEqual(waits[0], ("NeedsUserInput", ["salary"]))
        self.assertEqual(result["status"], "ReadyForSubmission")
        self.assertEqual(salary.data["value"], "85000")


class SubmissionTests(unittest.TestCase):
    def test_auto_submit_false_does_not_click(self):
        page = FakePage([FakeField(label="Full name", required=True)], submit_outcome="success")
        result = prepare_application(page, filled_profile(), auto_submit=False)
        self.assertEqual(result["status"], "ReadyForSubmission")
        self.assertEqual(page.buttons[0].clicks, 0)

    def test_auto_submit_true_clicks_only_fully_validated_local_form(self):
        page = FakePage([FakeField(label="Email", input_type="email", required=True)])
        result = prepare_application(page, filled_profile(), auto_submit=True)
        self.assertEqual(result["status"], "Submitted")
        self.assertEqual(page.buttons[0].clicks, 1)

    def test_auto_submit_true_never_clicks_a_real_page(self):
        page = FakePage([FakeField(label="Email", input_type="email", required=True)])
        page.url = "https://employer.example/jobs/apply"
        result = prepare_application(page, filled_profile(), auto_submit=True)
        self.assertEqual(result["status"], "ReadyForSubmission")
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(result["submission_blocked_reason"], "REAL_PAGE_SUBMISSION_DISABLED")
        self.assertEqual(page.buttons[0].clicks, 0)

    def test_auto_submit_true_does_not_click_when_required_answer_missing(self):
        page = FakePage([FakeField(label="Work authorization", required=True)])
        def close_for_test(*_args):
            page.closed = True
        result = prepare_application(page, filled_profile(), auto_submit=True,
                                     on_wait=close_for_test)
        self.assertEqual(result["status"], "NeedsUserInput")
        self.assertEqual(page.buttons[0].clicks, 0)

    def test_success_and_failure_result_signals(self):
        page = FakePage(body="Your application has been received")
        self.assertEqual(detect_application_result(page)["status"], "Submitted")
        failed = FakePage(body="Unable to submit your application")
        self.assertEqual(detect_application_result(failed)["status"], "Failed")

    def test_failure_signal_from_local_submit_is_returned(self):
        page = FakePage([FakeField(label="Email", input_type="email", required=True)], submit_outcome="failure")
        result = prepare_application(page, filled_profile(), auto_submit=True)
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["signal"], "submission_error_text")

    def test_submission_exception_is_recorded_with_useful_error(self):
        from worker.application import apply_to_job
        page = FakePage([FakeField(label="Email", input_type="email", required=True)])
        def broken_click():
            raise RuntimeError("local submit click failed")
        page.buttons[0].click = broken_click
        job = {"id": "local-submit-1", "company": "Local Fixture", "title": "Test Role", "url": page.url}
        with patch("worker.application.save_application"), \
             patch("worker.application.update_application_status") as update:
            result = apply_to_job(page, job, filled_profile(), auto_submit=True,
                                  job_key="url:local-submit", queue_id="local-submit-1")
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_reason"], "FORM_PREPARATION_FAILED")
        self.assertEqual(result["error"]["error_type"], "RuntimeError")
        self.assertIn("local submit click failed", result["error"]["error_message"])
        self.assertEqual(result["error"]["queue_id"], "local-submit-1")
        self.assertEqual(update.call_args.args[1], "Failed")

    def test_navigation_failure_is_returned_with_queue_identity(self):
        runner = load_runner_module()
        page = FakePage()
        page.url = "about:blank"
        def failed_navigation(*_args, **_kwargs):
            raise TimeoutError("local navigation timeout")
        page.goto = failed_navigation
        job = {"id": "local-nav-1", "title": "Test Role", "company": "Local Co",
               "url": "https://local.invalid/apply", "source": "local-test"}
        with patch.object(runner, "validate_job", return_value=(True, None)), \
             patch.object(runner, "find_protected_queue_duplicate", return_value=None), \
             patch.object(runner, "is_duplicate_application", return_value=(False, None)), \
             patch.object(runner, "load_applicant_profile", return_value=filled_profile()), \
             patch.object(runner, "save_application"), \
             patch.object(runner, "mark_job_status"), \
             patch.object(runner, "_record_failure") as record_failure:
            result = runner.process_job(page, job, auto_submit=False,
                                        navigation_policy=_TEST_NAVIGATION_POLICY)
        self.assertEqual(result["status"], "Retry")
        self.assertEqual(result["failure_reason"], "NAVIGATION_FAILED")
        self.assertEqual(record_failure.call_args.args[2], "NAVIGATION_FAILED")
        self.assertIn("local navigation timeout", record_failure.call_args.args[3])
        self.assertEqual(record_failure.call_args.args[0]["id"], "local-nav-1")

    def test_submission_result_timeout_is_reported(self):
        page = FakePage([FakeField(label="Email", input_type="email", required=True)], submit_outcome="none")
        with patch("worker.form_adapter.time.monotonic", side_effect=[0, 31]):
            result = prepare_application(page, filled_profile(), auto_submit=True)
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_reason"], "SUBMISSION_RESULT_NOT_CONFIRMED")
        self.assertEqual(page.buttons[0].clicks, 1)

    def test_unresolved_user_input_remains_paused_and_does_not_submit(self):
        page = FakePage([FakeField(label="Work authorization", required=True)])
        result = prepare_application(page, filled_profile(), auto_submit=True,
                                     on_wait=lambda *_args: setattr(page, "closed", True))
        self.assertEqual(result["status"], "NeedsUserInput")
        self.assertEqual(result["reason"], "BROWSER_CLOSED")
        self.assertEqual(page.buttons[0].clicks, 0)

    def test_auto_submit_default_is_false(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({"auto_submit": False}), encoding="utf-8")
            self.assertFalse(auto_submit_enabled(config))
            config.write_text(json.dumps({"auto_submit": True}), encoding="utf-8")
            self.assertTrue(auto_submit_enabled(config))

    def test_profile_loader_uses_local_structured_file_and_missing_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "applicant.json"
            profile.write_text(json.dumps({"name": "Test", "resume_path": None}), encoding="utf-8")
            self.assertIsNone(load_applicant_profile(profile)["resume_path"])


class ApplicationTrackingTests(unittest.TestCase):
    def test_application_records_validated_final_status(self):
        from worker.application import apply_to_job
        page = FakePage([FakeField(label="Email", input_type="email", required=True)])
        job = {"company": "Local Fixture", "job_title": "Test Role", "url": page.url, "source": "Test", "id": "local-1"}
        with patch("worker.application.save_application") as save, \
             patch("worker.application.update_application_status") as update:
            result = apply_to_job(page, job, filled_profile(), auto_submit=False,
                                  job_key="url:local-fixture", queue_id="local-1")
        self.assertEqual(result["status"], "ReadyForSubmission")
        save.assert_called_once()
        self.assertEqual(update.call_args.args[1], "ReadyForSubmission")
        self.assertTrue(update.call_args.kwargs["metadata"]["form_validated"])


def load_runner_module():
    """Import the runner for unit tests without requiring a browser installation."""
    if "worker.runner" in sys.modules:
        return sys.modules["worker.runner"]
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: None
    playwright.sync_api = sync_api
    with patch.dict(sys.modules, {"playwright": playwright, "playwright.sync_api": sync_api}):
        return importlib.import_module("worker.runner")


if __name__ == "__main__":
    unittest.main()
