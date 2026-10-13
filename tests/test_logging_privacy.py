"""Focused offline unit tests for logging privacy and error sanitization.

All tests use strictly synthetic placeholder data (RFC 2606 example.invalid).
No browser automation, no external network requests, and zero production data mutation.
"""

import io
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.data_safety import preserve_project_data_test
from worker.sanitizer import (
    FALLBACK_SAFE_MESSAGE,
    MAX_ERROR_LENGTH,
    sanitize_error_text,
    strip_url_query_and_fragment,
)


class LoggingPrivacyTests(unittest.TestCase):
    """Test suite for targeted privacy remediation and safe error formatting."""

    # 1. URL query values and fragments do not appear in sanitized output
    def test_url_query_and_fragment_stripped_from_error(self):
        dirty = "Navigation timeout at https://example.invalid/careers?access_token=SYNTHETIC_SECRET#step2"
        sanitized = sanitize_error_text(dirty)
        self.assertNotIn("access_token", sanitized)
        self.assertNotIn("SYNTHETIC_SECRET", sanitized)
        self.assertNotIn("#step2", sanitized)
        self.assertIn("https://example.invalid/careers", sanitized)

    def test_strip_url_query_and_fragment_helper(self):
        url = "https://example.invalid/apply?user=alice&session_id=SYNTHETIC_SESSION#target"
        clean = strip_url_query_and_fragment(url)
        self.assertEqual(clean, "https://example.invalid/apply")

    # 2. Synthetic tokens and credentials do not appear in sanitized output
    def test_bearer_token_and_secret_assignment_redacted(self):
        dirty_bearer = "Request failed: Authorization: Bearer SYNTHETIC_TEST_TOKEN_12345"
        sanitized_bearer = sanitize_error_text(dirty_bearer)
        self.assertNotIn("SYNTHETIC_TEST_TOKEN_12345", sanitized_bearer)
        self.assertIn("Bearer [REDACTED]", sanitized_bearer)

        dirty_key = "Connection refused with api_key: SYNTHETIC_SECRET_KEY and password=SYNTHETIC_PASS"
        sanitized_key = sanitize_error_text(dirty_key)
        self.assertNotIn("SYNTHETIC_SECRET_KEY", sanitized_key)
        self.assertNotIn("SYNTHETIC_PASS", sanitized_key)
        self.assertIn("[REDACTED]", sanitized_key)

    # 3. Synthetic email and phone patterns are redacted according to policy
    def test_email_and_phone_patterns_redacted(self):
        dirty_email = "Validation error for candidate: candidate@example.invalid failed schema"
        sanitized_email = sanitize_error_text(dirty_email)
        self.assertNotIn("candidate@example.invalid", sanitized_email)
        self.assertIn("[EMAIL_REDACTED]", sanitized_email)

        dirty_phone = "Invalid format for phone number: +1-555-0199 or (555) 012-3456"
        sanitized_phone = sanitize_error_text(dirty_phone)
        self.assertNotIn("555-0199", sanitized_phone)
        self.assertIn("[PHONE_REDACTED]", sanitized_phone)

    # 4. Sanitized messages obey the maximum length
    def test_sanitized_messages_obey_maximum_length(self):
        very_long = "NetworkError: " + ("very_long_error_detail_" * 30)
        sanitized = sanitize_error_text(very_long, max_length=100)
        self.assertLessEqual(len(sanitized), 103)  # 100 + "..."
        self.assertTrue(sanitized.endswith("..."))

    # 5. Unexpected input types do not raise an exception
    def test_unexpected_input_types_handled_safely(self):
        self.assertEqual(sanitize_error_text(None), "")
        self.assertEqual(sanitize_error_text(12345), "12345")
        self.assertEqual(sanitize_error_text(["error", 404]), "['error', 404]")
        self.assertEqual(sanitize_error_text({"code": 500}), "{'code': 500}")

        class Unstringable:
            def __str__(self):
                raise RuntimeError("Boom!")

        self.assertEqual(sanitize_error_text(Unstringable()), FALLBACK_SAFE_MESSAGE)

    # 6. Sanitization failures use a safe fallback
    def test_sanitization_fallback_on_internal_exception(self):
        with patch("re.sub", side_effect=Exception("Regex engine failed")):
            fallback_result = sanitize_error_text("some error message")
            self.assertEqual(fallback_result, FALLBACK_SAFE_MESSAGE)

    # 7. Error text persisted by updated code is sanitized before storage
    @preserve_project_data_test
    def test_runner_record_failure_persists_only_sanitized_error(self):
        from worker.runner import _record_failure
        from worker.job_queue import load_queue
        from worker.tracker import load_applications

        with tempfile.TemporaryDirectory() as temp:
            temp_queue = Path(temp) / "queue.json"
            temp_tracker = Path(temp) / "applications.json"

            # Create isolated test job
            test_job = {
                "id": "privacy-test-job-1",
                "title": "Frontend Developer",
                "company": "Synthetic Corp",
                "location": "Remote",
                "url": "https://example.invalid/apply?token=SYNTHETIC_URL_TOKEN#frag",
                "retry_count": 0,
            }

            with patch("worker.runner.mark_job_status") as mock_mark, \
                 patch("worker.runner.save_application") as mock_save, \
                 patch("worker.runner.update_application_status") as mock_update, \
                 patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:

                dirty_detail = "Failed: token=SYNTHETIC_DETAIL_TOKEN and email: candidate@example.invalid"
                _record_failure(test_job, "test-key-1", "PREPARATION_FAILED", detail=dirty_detail)

                # Verify mark_job_status received sanitized last_error
                last_error_arg = mock_mark.call_args[1]["last_error"]
                self.assertNotIn("SYNTHETIC_DETAIL_TOKEN", last_error_arg)
                self.assertNotIn("candidate@example.invalid", last_error_arg)
                self.assertIn("[REDACTED]", last_error_arg)
                self.assertIn("[EMAIL_REDACTED]", last_error_arg)

                # Verify URL in metadata is sanitized
                metadata_arg = mock_mark.call_args[1]["metadata"]
                clean_url = metadata_arg["last_failure"]["url"]
                self.assertNotIn("SYNTHETIC_URL_TOKEN", clean_url)
                self.assertEqual(clean_url, "https://example.invalid/apply")

                # 8. Verify the original unsanitized exception was not printed
                printed = mock_stdout.getvalue()
                self.assertNotIn("SYNTHETIC_DETAIL_TOKEN", printed)
                self.assertNotIn("candidate@example.invalid", printed)
                self.assertIn("[REDACTED]", printed)
                self.assertIn("[EMAIL_REDACTED]", printed)

    # 8. Original exception text is not printed or persisted by application.py
    def test_application_exception_handler_sanitizes_error_and_url(self):
        from worker.application import apply_to_job

        class FakeExplodingPage:
            url = "https://example.invalid/jobs/1?auth=SYNTHETIC_PAGE_AUTH"

        job = {"company": "Test Co", "title": "Developer", "url": "https://example.invalid/jobs/1"}
        applicant = {}

        with patch("worker.application.prepare_application", side_effect=ValueError("Token secret: SYNTHETIC_SECRET")), \
             patch("worker.application.save_application"), \
             patch("worker.application.update_application_status") as mock_tracker_update, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:

            result = apply_to_job(FakeExplodingPage(), job, applicant)
            self.assertEqual(result["status"], "Failed")

            # Check printed output
            printed = mock_stdout.getvalue()
            self.assertNotIn("SYNTHETIC_SECRET", printed)
            self.assertIn("[REDACTED]", printed)

            # Check metadata passed to tracker
            metadata = mock_tracker_update.call_args[1]["metadata"]
            error_info = metadata["last_failure"]
            self.assertNotIn("SYNTHETIC_SECRET", error_info["error_message"])
            self.assertNotIn("SYNTHETIC_PAGE_AUTH", error_info["url"])
            self.assertEqual(error_info["url"], "https://example.invalid/jobs/1")

    # 9. Safe generic messages remain understandable
    def test_safe_generic_messages_remain_understandable(self):
        clean_msg = "ElementNotFound: Unable to locate selector button[type='submit']"
        sanitized = sanitize_error_text(clean_msg)
        self.assertEqual(sanitized, clean_msg)
        self.assertIn("ElementNotFound", sanitized)

    # 10. Exception objects convert and format safely
    def test_exception_objects_format_safely(self):
        exc = ConnectionRefusedError("Connection refused on host: api_key=SECRET_123")
        sanitized = sanitize_error_text(exc)
        self.assertIn("ConnectionRefusedError:", sanitized)
        self.assertNotIn("SECRET_123", sanitized)
        self.assertIn("[REDACTED]", sanitized)


if __name__ == "__main__":
    unittest.main()
