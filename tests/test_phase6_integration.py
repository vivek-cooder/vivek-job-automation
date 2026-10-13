"""Browser-backed local integration and restart checks; all persistence is temporary."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from worker.ai.base import AIProviderError
from worker.ai.browser_agent import BrowserAgent
from worker.detector import detect_verification
from worker.job_queue import (
    add_job, get_job_key, get_next_job, load_queue, mark_job_status,
    recover_stale_jobs, write_queue,
)
from worker.sync_queue import evaluate_job, score_job
from worker.tracker import (
    is_duplicate_application, save_application, update_application_status,
)
from worker.navigation import NavigationPolicy

from data_safety import preserve_project_data
from test_phase2 import filled_profile


BASE = Path(__file__).resolve().parents[1]
TEST_NAVIGATION_POLICY = NavigationPolicy.for_local_tests()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _serve(directory):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(directory)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/test_application.html"


def _integrated_fixture(directory):
    """Use the existing local fixture with one deliberately unfamiliar field."""
    html = (BASE / "test_application.html").read_text(encoding="utf-8")
    unfamiliar = """
    <label for="contactNumber">Contact number</label>
    <input id="contactNumber" type="text" required>
    """
    html = html.replace("</form>", unfamiliar + "</form>", 1)
    html = html.replace(
        "    let verified = false;",
        """    let verified = false;
    const contactNumber = document.getElementById("contactNumber");
    const savedContactNumber = localStorage.getItem("phase6-contact-number");
    if (savedContactNumber) contactNumber.value = savedContactNumber;
    contactNumber.addEventListener("input", () => {
        localStorage.setItem("phase6-contact-number", contactNumber.value);
        if (!verified) document.querySelector(".verification").style.display = "block";
    });""",
        1,
    )
    path = Path(directory) / "test_application.html"
    path.write_text(html, encoding="utf-8")
    return path


def _ai_settings(*, retries=0, analyses=3):
    return SimpleNamespace(
        enabled=True, provider="mock", model="fixture", api_key="",
        min_confidence=0.90, max_actions_per_job=10,
        max_page_analysis=analyses, max_retries=retries, timeout_seconds=3,
    )


def _worker_storage(runner, queue_path, tracker_path):
    """Redirect every runner/application/tracker write and reject path leaks."""
    from worker import tracker as tracker_module

    def mark(job_id, status, **kwargs):
        kwargs["path"] = queue_path
        return mark_job_status(job_id, status, **kwargs)

    def save(*args, **kwargs):
        kwargs["path"] = tracker_path
        return save_application(*args, **kwargs)

    def update(*args, **kwargs):
        kwargs["path"] = tracker_path
        return update_application_status(*args, **kwargs)

    real_write = tracker_module._write

    def guarded_write(records, path):
        if Path(path).resolve() != Path(tracker_path).resolve():
            raise AssertionError(f"Tracker write escaped temporary test storage: {path}")
        return real_write(records, path)

    return [
        patch.object(runner, "load_queue", side_effect=lambda: load_queue(queue_path)),
        patch.object(runner, "mark_job_status", side_effect=mark),
        patch.object(runner, "save_application", side_effect=save),
        patch.object(runner, "update_application_status", side_effect=update),
        patch.object(runner, "is_duplicate_application",
                     side_effect=lambda job: is_duplicate_application(job, path=tracker_path)),
        patch.object(tracker_module, "_write", side_effect=guarded_write),
        patch.dict(runner.apply_to_job.__globals__, {
            "save_application": save, "update_application_status": update,
        }),
    ]


class Phase6BrowserIntegrationTests(unittest.TestCase):
    def test_worker_main_starts_with_clean_environment_and_local_empty_queue(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is unavailable in this interpreter")
        from worker.job_queue import get_waiting_job, recover_stale_jobs
        from worker.tracker import load_applications

        import worker.runner as runner

        class StopAfterStartup(Exception):
            pass

        with preserve_project_data(), tempfile.TemporaryDirectory() as temp:
            queue_path = Path(temp) / "queue.json"
            tracker_path = Path(temp) / "applications.json"
            queue_path.write_text("[]", encoding="utf-8")
            tracker_path.write_text("[]", encoding="utf-8")
            self.assertEqual(load_queue(queue_path), [])
            self.assertEqual(load_applications(tracker_path), [])
            def start_temporary_browser(playwright, navigation_policy=None):
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                return browser, context, context.new_page()

            with patch.object(runner, "sync_playwright", sync_playwright), \
                 patch.object(runner, "recover_stale_jobs",
                              side_effect=lambda **kwargs: recover_stale_jobs(queue_path, **kwargs)), \
                 patch.object(runner, "get_waiting_job",
                              side_effect=lambda: get_waiting_job(queue_path)), \
                 patch.object(runner, "get_next_job",
                              side_effect=lambda: get_next_job(queue_path)), \
                 patch.object(runner, "start_browser", side_effect=start_temporary_browser), \
                 patch.object(runner.time, "sleep", side_effect=StopAfterStartup):
                with self.assertRaises(StopAfterStartup):
                    runner.main()

    def test_worker_ai_human_restart_submission_and_duplicate_are_integrated(self):
        """Queue → worker → Phase 2 → mocked AI → human pause → resume → tracker."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is unavailable in this interpreter")

        import worker.runner as runner
        with preserve_project_data(), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _integrated_fixture(root)
            server, server_thread, url = _serve(root)
            queue_path = root / "application_queue.json"
            tracker_path = root / "applications.json"
            profile_path = root / "temporary-browser-profile"
            job = {
                "id": "phase6-e2e-001", "source": "phase6-local",
                "company": "Local Fixture", "title": "React Developer",
                "location": "Mumbai", "url": url, "status": "Queued",
            }
            added, record, reason = add_job(job, queue_path)
            self.assertTrue(added, reason)
            self.assertEqual(record["status"], "Queued")
            self.assertEqual(record["job_key"], get_job_key(job))

            class MockProvider:
                name = "mock-provider"
                model = "local-fixture"

                def __init__(self):
                    self.calls = 0
                    self.snapshots = []

                def analyze_page(self, snapshot):
                    self.calls += 1
                    self.snapshots.append(snapshot)
                    if self.calls == 1:
                        raise AIProviderError("AI_TIMEOUT")
                    target = next(
                        row["target_ref"] for row in snapshot["fields"]
                        if "contact number" in row["label"].casefold()
                    )
                    return {
                        "page_type": "job_application", "confidence": 0.99,
                        "human_verification": False, "ready_for_validation": True,
                        "fields": [{
                            "target_ref": target, "classification": "phone",
                            "confidence": 0.99, "action": "fill",
                            "option_label": None, "scroll_amount": None,
                        }],
                    }

            provider = MockProvider()
            agent_settings = _ai_settings(retries=1)
            human_notice = Mock(return_value={"success": True, "message_id": 7})
            result_notice = Mock(return_value={"success": True, "message_id": 8})
            run_number = {"value": 0}
            contexts = []
            from worker.runner import _make_wait_callbacks as real_make_callbacks
            self.assertIs(real_make_callbacks.__globals__, runner.__dict__)

            def callbacks(job_data, job_key, tracker_job):
                wait, resume = real_make_callbacks(job_data, job_key, tracker_job)
                this_run = run_number["value"]

                def wait_with_test_human(status, reason, details, current_url):
                    wait(status, reason, details, current_url)
                    stored = load_queue(queue_path)[0]
                    self.assertEqual(
                        stored["status"], "WaitingForHuman",
                        f"queue did not persist wait transition: {stored!r}",
                    )
                    self.assertTrue(stored.get("waiting_event_id"))
                    if this_run == 1:
                        # Simulate worker/browser termination with the local gate still visible.
                        contexts[-1].close()
                    else:
                        # Test harness acts as a person on this fake local fixture only.
                        page = contexts[-1].pages[0]
                        page.locator("#verificationCode").fill("123456")
                        page.locator("#verifyButton").click()
                        self.assertIsNone(detect_verification(page))

                return wait_with_test_human, resume

            patchers = _worker_storage(runner, queue_path, tracker_path)
            started_patchers = []
            try:
                for patcher in patchers:
                    patcher.start()
                    started_patchers.append(patcher)
                with sync_playwright() as playwright:
                    try:
                        context = playwright.chromium.launch_persistent_context(
                            str(profile_path), headless=True,
                        )
                    except Exception as exc:
                        self.skipTest(f"Local Chromium launch blocked: {type(exc).__name__}: {exc}")
                    contexts.append(context)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(url, wait_until="load")
                    # Hide the fixture's fake gate until the mocked AI fills its unknown field.
                    page.locator(".verification").evaluate(
                        "element => element.style.display = 'none'"
                    )

                    def make_agent(queue_id=None):
                        return BrowserAgent(provider, agent_settings, job_id=queue_id)

                    with patch.object(runner, "_make_wait_callbacks", side_effect=callbacks), \
                         patch.dict(runner.apply_to_job.__globals__, {
                             "create_browser_agent": make_agent,
                         }), \
                         patch("worker.telegram.send_human_intervention_notification", human_notice), \
                         patch("worker.telegram.send_application_result_notification", result_notice):
                        run_number["value"] = 1
                        paused = runner.process_job(
                            page, job, applicant=filled_profile(), auto_submit=True,
                            navigation_policy=TEST_NAVIGATION_POLICY,
                        )
                        self.assertEqual(paused["status"], "WaitingForHuman")
                        paused_queue = load_queue(queue_path)[0]
                        self.assertEqual(paused_queue["status"], "WaitingForHuman")
                        self.assertEqual(paused_queue["waiting_reason"], "CAPTCHA_REQUIRED")
                        self.assertTrue(paused_queue.get("human_notification_attempted_at"))
                        self.assertEqual(provider.calls, 2)  # transient timeout then structured plan
                        self.assertEqual(len(load_queue(queue_path)), 1)
                        self.assertEqual(len(json.loads(tracker_path.read_text(encoding="utf-8"))), 1)
                        contexts[-1].close()

                        # Re-open the same temporary browser profile and resume persisted work.
                        contexts.append(playwright.chromium.launch_persistent_context(
                            str(profile_path), headless=True,
                        ))
                        page = contexts[-1].pages[0] if contexts[-1].pages else contexts[-1].new_page()
                        page.goto(url, wait_until="load")
                        self.assertEqual(
                            page.locator("#contactNumber").input_value(), "0000000000"
                        )
                        waiting_job = load_queue(queue_path)[0]
                        run_number["value"] = 2
                        submitted = runner.process_job(
                            page, waiting_job, applicant=filled_profile(),
                            auto_submit=True, resume=True,
                            navigation_policy=TEST_NAVIGATION_POLICY,
                        )

                        self.assertEqual(submitted["status"], "Submitted")
                        self.assertTrue(page.locator("#submitSuccess").is_visible())
                        self.assertEqual(human_notice.call_count, 1)
                        self.assertEqual(result_notice.call_count, 1)
                        self.assertEqual(provider.calls, 2)
                        final_queue = load_queue(queue_path)
                        history = json.loads(tracker_path.read_text(encoding="utf-8"))
                        self.assertEqual(len(final_queue), 1)
                        self.assertEqual(final_queue[0]["id"], job["id"])
                        self.assertEqual(final_queue[0]["url"], url)
                        self.assertEqual(final_queue[0]["original_url"], url)
                        self.assertEqual(final_queue[0]["application_url"], url)
                        self.assertEqual(final_queue[0]["status"], "Submitted")
                        self.assertEqual(len(history), 1)
                        self.assertEqual(history[0]["job_key"], get_job_key(job))
                        self.assertEqual(history[0]["queue_id"], job["id"])
                        self.assertEqual(history[0]["original_url"], url)
                        self.assertEqual(history[0]["application_url"], url)
                        self.assertEqual(history[0]["status"], "Submitted")
                        self.assertTrue(history[0].get("submitted_at"))
                        self.assertTrue(final_queue[0]["last_wait_event"].get("resumed_at"))

                        # Replay the same queue ID; it must not create an attempt or change history.
                        duplicate = runner.process_job(
                            page, job, applicant=filled_profile(), auto_submit=True,
                            navigation_policy=TEST_NAVIGATION_POLICY,
                        )
                        self.assertEqual(duplicate["status"], "Skipped")
                        self.assertEqual(len(load_queue(queue_path)), 1)
                        self.assertEqual(len(json.loads(tracker_path.read_text(encoding="utf-8"))), 1)
                        self.assertEqual(human_notice.call_count, 1)
                        self.assertEqual(result_notice.call_count, 1)
            finally:
                for patcher in reversed(started_patchers):
                    patcher.stop()
                for context in contexts:
                    try:
                        context.close()
                    except Exception:
                        pass
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)

    def test_auto_submit_false_fills_and_validates_without_clicking_local_fixture(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is unavailable in this interpreter")
        from worker.form_adapter import prepare_application

        fixture = BASE / "test_application.html"
        with preserve_project_data(), sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Local Chromium launch blocked: {type(exc).__name__}: {exc}")
            try:
                page = browser.new_page()
                page.goto(fixture.as_uri())
                # Test harness completes this fixture's fake manual verification step.
                page.locator("#verificationCode").fill("123456")
                page.locator("#verifyButton").click()
                result = prepare_application(page, filled_profile(), auto_submit=False,
                                             navigation_policy=TEST_NAVIGATION_POLICY)
                self.assertEqual(result["status"], "ReadyForSubmission")
                self.assertTrue(result["valid_for_submission"])
                self.assertEqual(page.locator("#submitButton").inner_text(), "Submit Application")
                self.assertFalse(page.locator("#submitSuccess").is_visible())
            finally:
                browser.close()

    def test_sensitive_local_question_is_not_guessed_or_submitted(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is unavailable in this interpreter")
        from worker.form_adapter import prepare_application

        with preserve_project_data(), tempfile.TemporaryDirectory() as temp, sync_playwright() as playwright:
            form = Path(temp) / "sensitive.html"
            form.write_text(
                "<form><h1>Job Application</h1><label>Are you legally authorized to work here? "
                "<input id='authorization' required></label>"
                "<button type='submit'>Submit Application</button></form>",
                encoding="utf-8",
            )
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Local Chromium launch blocked: {type(exc).__name__}: {exc}")
            try:
                page = browser.new_page()
                page.goto(form.as_uri())

                def stop_for_person(*_args):
                    page.close()

                result = prepare_application(
                    page, filled_profile(), auto_submit=True, on_wait=stop_for_person,
                    navigation_policy=TEST_NAVIGATION_POLICY,
                )
                self.assertEqual(result["status"], "NeedsUserInput")
                self.assertEqual(result["reason"], "BROWSER_CLOSED")
                self.assertEqual(result["missing_fields"], ["work_authorization"])
            finally:
                browser.close()


class Phase6StateRegressionTests(unittest.TestCase):
    def test_worker_restart_state_matrix_preserves_or_recovers_each_status(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "application_queue.json"
            old = "2020-01-01T00:00:00+00:00"
            statuses = ["Queued", "Started", "Processing", "WaitingForHuman",
                        "ReadyForSubmission", "Failed", "Retry"]
            records = [
                {"id": f"state-{index}", "title": "Local role", "company": "Local Co",
                 "source": "phase6", "url": f"file:///state-{index}.html",
                 "status": status, "created_at": old, "updated_at": old,
                 "retry_count": 0}
                for index, status in enumerate(statuses)
            ]
            write_queue(records, path)
            recovered = recover_stale_jobs(path, stale_after_seconds=1)
            by_id = {record["id"]: record for record in load_queue(path)}
            self.assertEqual(
                {record["id"] for record in recovered},
                {"state-1", "state-2", "state-4"},
            )
            self.assertEqual(by_id["state-0"]["status"], "Queued")
            self.assertEqual(by_id["state-1"]["status"], "Retry")
            self.assertEqual(by_id["state-2"]["status"], "Retry")
            self.assertEqual(by_id["state-3"]["status"], "WaitingForHuman")
            self.assertEqual(by_id["state-4"]["status"], "Retry")
            self.assertEqual(by_id["state-5"]["status"], "Failed")
            self.assertEqual(by_id["state-6"]["status"], "Retry")
            self.assertEqual(by_id["state-1"]["retry_count"], 1)
            self.assertEqual(get_next_job(path)["id"], "state-0")

    def test_exact_requested_suitability_examples(self):
        examples = [
            ("React Developer", "Mumbai", "Entry level", False),
            ("Frontend Developer", "Thane", "0-1 years", False),
            ("UI/UX Designer", "Navi Mumbai", "Fresher", False),
            ("Remote Frontend Developer", "Remote", "", False),
            ("Fresher React Developer", "Mumbai", "0-1 years", False),
            ("Accountant", "Mumbai", "Entry level", True),
        ]
        for title, location, experience, rejected in examples:
            with self.subTest(title=title):
                score, _reason = score_job({
                    "title": title, "location": location, "experience": experience,
                })
                result = evaluate_job({
                    "title": title, "location": location, "experience": experience,
                })
                self.assertEqual(result.get("status") == "Skipped", rejected)
                if rejected:
                    self.assertLess(score, 0)
                else:
                    self.assertGreater(score, 0)


if __name__ == "__main__":
    unittest.main()
