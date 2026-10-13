"""Phase 3 Telegram tests use mocked HTTP and local worker state only."""

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from io import BytesIO

from worker import telegram
from tests.test_phase2 import FakeField, FakePage, filled_profile, load_runner_module
from tests.data_safety import preserve_project_data_test
from worker.navigation import NavigationPolicy


_TEST_NAVIGATION_POLICY = NavigationPolicy.for_local_tests()


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class TelegramApiTests(unittest.TestCase):
    def setUp(self):
        telegram._unavailable_logged = True
        self.env = patch.dict(os.environ, {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "fake-token-for-test",
            "TELEGRAM_CHAT_ID": "fake-chat-id",
        }, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_disabled_and_missing_credentials_are_nonfatal(self):
        self.assertEqual(telegram.send_message("test", enabled=False)["error"], "TELEGRAM_DISABLED")
        with patch.dict(os.environ, {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=True):
            self.assertEqual(telegram.send_message("test")["error"], "TELEGRAM_NOT_CONFIGURED")
        with patch.dict(os.environ, {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "fake-only"}, clear=True):
            self.assertEqual(telegram.send_message("test")["error"], "TELEGRAM_NOT_CONFIGURED")
        with patch.dict(os.environ, {"TELEGRAM_ENABLED": "true", "TELEGRAM_CHAT_ID": "fake-only"}, clear=True):
            self.assertEqual(telegram.send_message("test")["error"], "TELEGRAM_NOT_CONFIGURED")

    def test_success_posts_to_official_api_and_returns_message_id(self):
        with patch("worker.telegram.urlopen", return_value=FakeResponse({
            "ok": True, "result": {"message_id": 42},
        })) as post:
            result = telegram.send_message("local test")
        self.assertEqual(result, {"success": True, "message_id": 42})
        request = post.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://api.telegram.org/botfake-token-for-test/"))
        self.assertEqual(json.loads(request.data)["chat_id"], "fake-chat-id")
        self.assertEqual(post.call_args.kwargs["timeout"], telegram.DEFAULT_TIMEOUT_SECONDS)

    def test_telegram_api_error_does_not_expose_response_details(self):
        with patch("worker.telegram.urlopen", return_value=FakeResponse({"ok": False, "error_code": 500, "description": "failure"})):
            result = telegram.send_message("test")
        self.assertEqual(result, {"success": False, "error": "TELEGRAM_API_ERROR"})

    def test_http_error_is_structured(self):
        error = HTTPError("https://api.telegram.org/fake", 503, "unavailable", {}, BytesIO(b"opaque"))
        with patch("worker.telegram.urlopen", side_effect=error):
            result = telegram.send_message("test")
        self.assertEqual(result["error"], "TELEGRAM_HTTP_ERROR")

    def test_invalid_credentials_response_is_safe(self):
        with patch("worker.telegram.urlopen", return_value=FakeResponse({"ok": False, "error_code": 401})):
            result = telegram.send_message("test")
        self.assertEqual(result["error"], "TELEGRAM_INVALID_CREDENTIALS")
        self.assertNotIn("fake-token-for-test", str(result))

    def test_invalid_chat_id_response_is_safe(self):
        with patch("worker.telegram.urlopen", return_value=FakeResponse({"ok": False, "error_code": 400})):
            result = telegram.send_message("test")
        self.assertEqual(result["error"], "TELEGRAM_INVALID_CHAT_ID")

    def test_timeout_and_connection_failure_are_structured(self):
        with patch("worker.telegram.urlopen", side_effect=TimeoutError("fake-token-for-test")):
            timeout = telegram.send_message("test")
        with patch("worker.telegram.urlopen", side_effect=OSError("network unavailable")):
            connection = telegram.send_message("test")
        self.assertEqual(timeout["error"], "TELEGRAM_TIMEOUT")
        self.assertEqual(connection["error"], "TELEGRAM_CONNECTION_ERROR")
        self.assertNotIn("fake-token-for-test", str(timeout))

    def test_human_notification_format_and_safe_url(self):
        job = {
            "id": "job-123", "company": "Example Technologies",
            "title": "React Developer", "location": "Mumbai",
            "url": "https://jobs.example/apply?id=job-123&access_token=private#fragment",
        }
        with patch("worker.telegram.send_message", return_value={"success": True}) as send:
            telegram.send_human_intervention_notification(job, "CAPTCHA_REQUIRED")
        message = send.call_args.args[0]
        for expected in ("Human action required", "Example Technologies", "React Developer",
                         "Mumbai", "CAPTCHA verification required", "Job ID: job-123",
                         "already-open desktop browser", "Employer reference link",
                         "https://jobs.example/apply?id=job-123"):
            self.assertIn(expected, message)
        self.assertNotIn("private", message)
        self.assertNotIn("access_token", message)

    def test_ready_and_final_notification_formats(self):
        job = {"id": "local-9", "company": "Local Co", "title": "Frontend Developer"}
        with patch("worker.telegram.send_message", return_value={"success": True}) as send:
            telegram.send_application_ready_notification(job)
            telegram.send_application_result_notification(job, "Submitted")
            telegram.send_application_result_notification(job, "Failed", "NAVIGATION_FAILED")
        messages = [call.args[0] for call in send.call_args_list]
        self.assertIn("AUTO_SUBMIT is disabled", messages[0])
        self.assertIn("APPLICATION SUBMITTED", messages[1])
        self.assertIn("APPLICATION FAILED", messages[2])
        self.assertIn("NAVIGATION_FAILED", messages[2])

    def test_wait_until_clear_bounded_timeout_and_state_preservation(self):
        from worker.form_adapter import _wait_until_clear, prepare_application
        page = FakePage([], body="Verify you are human")
        wait_calls = []
        resume_calls = []

        def on_wait(status, reason, details, url):
            wait_calls.append((status, reason, details, url))

        def on_resume(url):
            resume_calls.append(url)

        # 1. Direct _wait_until_clear timeout test
        cleared = _wait_until_clear(
            page, filled_profile(), on_wait, on_resume,
            "WaitingForHuman", "CAPTCHA_REQUIRED", {},
            timeout_seconds=0.05,
        )
        self.assertFalse(cleared)
        self.assertEqual(len(wait_calls), 1)
        self.assertEqual(wait_calls[0][0], "WaitingForHuman")
        self.assertEqual(wait_calls[0][1], "CAPTCHA_REQUIRED")
        self.assertEqual(len(resume_calls), 0)

        # 2. prepare_application preserves WaitingForHuman and challenge reason on timeout
        result = prepare_application(
            page, filled_profile(),
            on_wait=on_wait, on_resume=on_resume,
            wait_timeout_seconds=0.05,
            navigation_policy=_TEST_NAVIGATION_POLICY,
        )
        self.assertEqual(result["status"], "WaitingForHuman")
        self.assertEqual(result["reason"], "CAPTCHA_REQUIRED")

        # 3. prepare_application preserves NeedsUserInput on unresolved fields timeout
        page_fields = FakePage(
            [FakeField(label="Unknown Field", name="unknown_opt", required=True)],
            body="Application form",
        )
        result_input = prepare_application(
            page_fields, filled_profile(),
            on_wait=on_wait, on_resume=on_resume,
            wait_timeout_seconds=0.05,
            navigation_policy=_TEST_NAVIGATION_POLICY,
        )
        self.assertEqual(result_input["status"], "NeedsUserInput")
        self.assertEqual(result_input["reason"], "REQUIRED_FIELDS_NEED_USER_INPUT")

        # 4. If browser closed, reason reflects BROWSER_CLOSED
        page_closed = FakePage([], body="Verify you are human")
        page_closed.closed = True
        result_closed = prepare_application(
            page_closed, filled_profile(),
            on_wait=on_wait, on_resume=on_resume,
            wait_timeout_seconds=10,
            navigation_policy=_TEST_NAVIGATION_POLICY,
        )
        self.assertEqual(result_closed["status"], "WaitingForHuman")

        # 5. When gate clears before timeout, returns successfully
        page_cleared = FakePage([], body="Verify you are human")
        page_cleared.wait_for_timeout = lambda _ms: setattr(page_cleared, "body", "Welcome to form")
        cleared_res = _wait_until_clear(
            page_cleared, filled_profile(), on_wait, on_resume,
            "WaitingForHuman", "CAPTCHA_REQUIRED", {},
            timeout_seconds=5,
        )
        self.assertTrue(cleared_res)
        self.assertEqual(len(resume_calls), 1)


class TelegramWorkerIntegrationTests(unittest.TestCase):
    @preserve_project_data_test
    def test_wait_notify_dedupe_restart_resume_and_submit_on_local_mock(self):
        from worker.job_queue import add_job, load_queue, mark_job_status
        from worker.tracker import save_application, update_application_status

        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as temp:
            queue_path = Path(temp) / "queue.json"
            tracker_path = Path(temp) / "applications.json"
            production_tracker = Path(__file__).resolve().parents[1] / "data" / "applications.json"
            production_hash_before = hashlib.sha256(production_tracker.read_bytes()).hexdigest()
            job = {
                "id": "phase3-local-1", "source": "local-test",
                "company": "Local Fixture", "title": "Test Role",
                "location": "Local", "url": "file:///local/test_application.html",
            }
            added, _, error = add_job(job, queue_path)
            self.assertTrue(added, error)

            def mark(job_id, status, **kwargs):
                kwargs["path"] = queue_path
                return mark_job_status(job_id, status, **kwargs)

            def save(*args, **kwargs):
                kwargs["path"] = tracker_path
                return save_application(*args, **kwargs)

            def update(*args, **kwargs):
                kwargs["path"] = tracker_path
                return update_application_status(*args, **kwargs)

            from worker import tracker as tracker_module
            real_tracker_write = tracker_module._write
            def isolated_tracker_write(records, path):
                if Path(path).resolve() != tracker_path.resolve():
                    raise AssertionError(f"Integration test attempted to write outside its temporary tracker: {path}")
                return real_tracker_write(records, path)

            page = FakePage(
                [FakeField(label="Email", input_type="email", required=True)],
                body="Verify you are human",
            )
            page.url = job["url"]
            page.wait_for_timeout = lambda _ms: setattr(page, "closed", True)
            human_send = Mock(return_value={"success": True, "message_id": 17})
            result_send = Mock(return_value={"success": True, "message_id": 18})

            with patch.object(runner, "load_queue", side_effect=lambda: load_queue(queue_path)), \
                 patch.object(runner, "mark_job_status", side_effect=mark), \
                 patch.object(runner, "save_application", side_effect=save), \
                 patch.object(runner, "update_application_status", side_effect=update), \
                 patch.object(runner, "find_protected_queue_duplicate", return_value=None), \
                 patch.object(runner, "is_duplicate_application", return_value=(False, None)), \
                 patch.dict(runner.apply_to_job.__globals__, {
                     "save_application": save,
                     "update_application_status": update,
                 }), \
                 patch("worker.tracker._write", side_effect=isolated_tracker_write), \
                 patch.object(telegram, "send_human_intervention_notification", human_send), \
                 patch.object(telegram, "send_application_result_notification", result_send):
                first = runner.process_job(page, job, applicant=filled_profile(), auto_submit=True,
                                           navigation_policy=_TEST_NAVIGATION_POLICY)
                queue = load_queue(queue_path)
                self.assertEqual(first["status"], "WaitingForHuman")
                self.assertEqual(queue[0]["status"], "WaitingForHuman")
                self.assertEqual(queue[0]["waiting_reason"], "CAPTCHA_REQUIRED")
                self.assertEqual(queue[0]["current_url"], job["url"])
                self.assertEqual(queue[0]["job_key"], "id:local-test:phase3-local-1")
                self.assertTrue(queue[0]["human_notification_sent_at"])
                event_id = queue[0]["waiting_event_id"]
                detected_at = queue[0]["detected_at"]
                self.assertTrue(page.closed)

                # Simulate a restart: the queue still carries the event/attempt marker.
                page.closed = False
                page.body = "Verify you are human"
                page.wait_for_timeout = lambda _ms: setattr(page, "body", "Application form ready")
                waiting_job = load_queue(queue_path)[0]
                resumed = runner.process_job(page, waiting_job, applicant=filled_profile(),
                                             auto_submit=True, resume=True,
                                             navigation_policy=_TEST_NAVIGATION_POLICY)

            self.assertEqual(resumed["status"], "Submitted")
            self.assertEqual(human_send.call_count, 1)
            self.assertEqual(result_send.call_count, 1)
            final_queue = load_queue(queue_path)[0]
            self.assertEqual(final_queue["status"], "Submitted")
            self.assertIn("telegram_submitted_notification", final_queue)
            self.assertEqual(final_queue["last_wait_event"]["waiting_event_id"], event_id)
            self.assertEqual(final_queue["last_wait_event"]["detected_at"], detected_at)
            self.assertTrue(final_queue["last_wait_event"]["resumed_at"])
            history = json.loads(tracker_path.read_text(encoding="utf-8"))
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "Submitted")
            self.assertTrue(history[0]["last_wait_event"]["human_notification_sent_at"])
            self.assertEqual(
                hashlib.sha256(production_tracker.read_bytes()).hexdigest(),
                production_hash_before,
                "Local integration test must not alter the production tracker",
            )

    def test_ready_and_failure_notifications_are_deduplicated(self):
        runner = load_runner_module()
        job = {"id": "local-ready-2", "title": "UI Developer", "company": "Local Co"}
        tracker_job = dict(job, job_title=job["title"])
        queue_record = dict(job)
        def mark(_job_id, status, *, metadata=None, **_kwargs):
            queue_record["status"] = status
            queue_record.update(metadata or {})
        with patch.object(runner, "load_queue", side_effect=lambda: [dict(queue_record)]), \
             patch.object(runner, "mark_job_status", side_effect=mark), \
             patch.object(runner, "update_application_status"):
            ready_sender = Mock(return_value={"success": True, "message_id": 1})
            runner._notify_once(job, "", tracker_job, "ready", ready_sender, tracker_job)
            runner._notify_once(job, "", tracker_job, "ready", ready_sender, tracker_job)
            self.assertEqual(ready_sender.call_count, 1)
            self.assertTrue(queue_record["telegram_ready_notification"]["sent_at"])

            failed_sender = Mock(return_value={"success": True, "message_id": 2})
            runner._notify_once(job, "", tracker_job, "failed", failed_sender,
                                tracker_job, "Failed", "NAVIGATION_FAILED")
            runner._notify_once(job, "", tracker_job, "failed", failed_sender,
                                tracker_job, "Failed", "NAVIGATION_FAILED")
            self.assertEqual(failed_sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
