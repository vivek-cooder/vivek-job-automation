"""Local-only tests for conservative application-action detection and routing."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from tests.data_safety import preserve_project_data_test
from worker.detector import detect_verification
from worker.form_adapter import (
    find_application_link, identify_application_link, prepare_application,
)
from worker.navigation import NavigationPolicy


BASE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE = {
    "name": "Local Fixture Applicant", "email": "local@example.invalid",
    "phone": "0000000000", "location": "Mumbai", "resume_path": None,
}
JOB_CONTEXT = (
    "<h1>Associate Engineer - FrontEnd</h1>"
    "<p>Job description: Frontend role. Experience: 0-2 years. Responsibilities included.</p>"
)


class ApplicationLinkDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")
        cls.manager = sync_playwright()
        cls.playwright = cls.manager.start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.manager.__exit__(None, None, None)
            raise unittest.SkipTest(f"Local Chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "manager"):
            cls.manager.__exit__(None, None, None)

    def setUp(self):
        self.page = self.browser.new_page()
        self.navigation_policy = NavigationPolicy.for_local_tests()
        self._navigation_policy_patches = [
            patch("worker.form_adapter.configured_policy", return_value=self.navigation_policy),
            patch("worker.runner.configured_policy", return_value=self.navigation_policy),
        ]
        for patcher in self._navigation_policy_patches:
            patcher.start()
        # Establish a local base URL so relative fixture hrefs resolve normally.
        self.page.goto((FIXTURES / "accenture_like_listing.html").as_uri())

    def tearDown(self):
        for patcher in self._navigation_policy_patches:
            patcher.stop()
        if not self.page.is_closed():
            self.page.close()

    def test_positive_application_action_labels(self):
        labels = (
            "Apply", "Apply Now", "Apply for this job", "Apply for Job",
            "Apply to this position", "Start Application", "Start your application",
            "Submit Application",
        )
        for index, label in enumerate(labels):
            with self.subTest(label=label):
                self.page.set_content(
                    f"{JOB_CONTEXT}<a href='apply/action-{index}'>{label}</a>"
                )
                result = identify_application_link(self.page)
                self.assertEqual(result["status"], "Selected")
                self.assertEqual(result["url"], f"apply/action-{index}")

    def test_accessible_label_title_and_button_destinations_are_supported(self):
        controls = (
            '<a href="apply/aria" aria-label="Apply for this job">Open</a>',
            '<a href="apply/title" title="Apply for Job">More</a>',
            '<button type="button" data-href="apply/button" aria-label="Start your application">Go</button>',
            '<button type="button" data-application-url="apply/labelled" aria-labelledby="action-label">Go</button><span id="action-label">Apply to this position</span>',
        )
        expected = ("apply/aria", "apply/title", "apply/button", "apply/labelled")
        for control, href in zip(controls, expected):
            with self.subTest(href=href):
                self.page.set_content(f"{JOB_CONTEXT}{control}")
                self.assertEqual(identify_application_link(self.page)["url"], href)

    def test_unrelated_actions_and_prose_are_not_application_links(self):
        labels = (
            "Contact Us", "Contact", "Learn More", "View Details", "Sign In",
            "Login", "Register", "Subscribe", "Share", "Save Job", "Back", "Search",
        )
        for index, label in enumerate(labels):
            with self.subTest(label=label):
                self.page.set_content(
                    f"{JOB_CONTEXT}<a href='apply/{index}'>{label}</a>"
                )
                self.assertNotEqual(identify_application_link(self.page)["status"], "Selected")

        prose = (
            "How to apply", "We apply modern technologies", "Apply these guidelines",
            "Application deadline",
        )
        for text in prose:
            with self.subTest(prose=text):
                self.page.set_content(f"{JOB_CONTEXT}<p>{text}</p><a href='apply/prose'>Details</a>")
                self.assertIsNone(find_application_link(self.page))

    def test_newsletter_contact_and_unrelated_links_do_not_displace_clear_apply_link(self):
        self.page.set_content(f"""{JOB_CONTEXT}
          <form id="newsletter"><h2>Newsletter</h2><input type="email">
            <button type="submit">Subscribe</button></form>
          <form id="contact"><h2>Contact Us</h2><input type="email"></form>
          <a href="/contact">Contact Us</a><a href="/learn-more">Learn More</a>
          <a id="apply" href="apply/frontend">Apply for this job</a>""")
        self.assertEqual(find_application_link(self.page), "apply/frontend")
        self.assertEqual(self.page.locator("#newsletter input").input_value(), "")
        self.assertEqual(self.page.locator("#contact input").input_value(), "")

    def test_duplicate_destination_is_deduplicated_and_distinct_targets_stop(self):
        app_url = (FIXTURES / "accenture_like_application.html").as_uri()
        self.page.set_content(f"{JOB_CONTEXT}<a href='accenture_like_application.html'>Apply Now</a>"
                              f"<a href='{app_url}'>Apply for this job</a>")
        self.assertEqual(identify_application_link(self.page)["status"], "Selected")

        self.page.set_content(f"{JOB_CONTEXT}<a href='apply/one'>Apply Now</a>"
                              "<a href='apply/two'>Apply for this job</a>")
        result = identify_application_link(self.page)
        self.assertEqual(result["status"], "NeedsUserInput")
        self.assertEqual(result["reason"], "AMBIGUOUS_APPLICATION_LINK")
        self.assertIsNone(find_application_link(self.page))

    def test_no_link_and_unrelated_destination_fail_closed(self):
        self.page.set_content(f"{JOB_CONTEXT}<a href='/contact'>Apply for this job</a>")
        self.assertNotEqual(identify_application_link(self.page)["status"], "Selected")
        self.page.set_content(f"{JOB_CONTEXT}<p>No application action is available.</p>")
        self.assertEqual(identify_application_link(self.page)["status"], "Unsupported")

    def test_application_link_to_unauthorized_external_host_is_not_followed_or_filled(self):
        self.page.set_content(
            f"{JOB_CONTEXT}<a href='https://attacker.invalid/apply'>Apply for this job</a>"
        )
        result = prepare_application(
            self.page, PROFILE, auto_submit=False,
            navigation_policy=self.navigation_policy,
        )
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_reason"], "UNAUTHORIZED_HOST")
        self.assertTrue(self.page.url.startswith("file:"))

    @preserve_project_data_test
    def test_accenture_like_listing_runs_through_worker_to_manual_ready_state(self):
        from worker import application, runner

        job = {
            "id": "controlled-apply-link-fixture", "source": "local-fixture",
            "company": "Local Fixture Company", "title": "Associate Engineer - FrontEnd",
            "url": (FIXTURES / "accenture_like_listing.html").as_uri(),
        }
        records = []
        with patch.object(runner, "load_queue", return_value=[]), \
             patch.object(runner, "find_protected_queue_duplicate", return_value=None), \
             patch.object(runner, "is_duplicate_application", return_value=(False, None)), \
             patch.object(runner, "mark_job_status", side_effect=lambda *a, **kw: records.append((a, kw))), \
             patch.object(runner, "save_application"), \
             patch.object(runner, "update_application_status"), \
             patch.object(runner, "_notify_once"), \
             patch.object(runner, "_wait_for_manual_result", return_value={"status": "ReadyForSubmission"}), \
             patch.object(application, "save_application"), \
             patch.object(application, "update_application_status"), \
             patch.object(application, "create_browser_agent", return_value=None):
            result = runner.process_job(self.page, job, applicant=PROFILE, auto_submit=False,
                                        navigation_policy=self.navigation_policy)

        application_url = (FIXTURES / "accenture_like_application.html").as_uri()
        self.assertEqual(result["status"], "ReadyForSubmission")
        self.assertEqual(self.page.url, application_url)
        self.assertEqual(job["application_url"], application_url)
        ready_metadata = [kwargs.get("metadata", {}) for args, kwargs in records
                          if len(args) > 1 and args[1] == "ReadyForSubmission"]
        self.assertTrue(ready_metadata)
        self.assertTrue(any(item.get("original_url") == job["url"]
                            and item.get("application_url") == application_url
                            for item in ready_metadata))
        self.assertEqual(self.page.locator("[name=full_name]").input_value(), PROFILE["name"])
        self.assertEqual(self.page.locator("[name=applicant_email]").input_value(), PROFILE["email"])
        self.assertEqual(self.page.locator("[name=phone]").input_value(), PROFILE["phone"])
        self.assertEqual(self.page.locator("[name=newsletter_email]").count(), 0)
        for name in ("resume", "portfolio", "linkedin"):
            field = self.page.locator(f"[name={name}]")
            self.assertEqual(field.input_value() if name != "resume" else "", "")
        self.assertNotEqual(self.page.locator("body").get_attribute("data-submit-clicked"), "yes")

    def test_403_browser_challenge_stops_before_form_identification(self):
        fixture = (FIXTURES / "security_challenge.html").read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(fixture)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = self.page.goto(f"http://127.0.0.1:{server.server_port}/job", wait_until="load")
            self.assertEqual(response.status, 403)
            self.assertEqual(detect_verification(self.page), "SECURITY_CHALLENGE_REQUIRED")
            waits = []

            def close_at_gate(status, reason, *_args):
                waits.append((status, reason))
                self.page.close()

            with patch("worker.form_adapter.identify_application_form") as identify_form:
                result = prepare_application(self.page, PROFILE, auto_submit=False, on_wait=close_at_gate)
            identify_form.assert_not_called()
            self.assertEqual(result["status"], "WaitingForHuman")
            self.assertEqual(result["reason"], "SECURITY_CHALLENGE_REQUIRED")
            self.assertEqual(waits, [("WaitingForHuman", "SECURITY_CHALLENGE_REQUIRED")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
