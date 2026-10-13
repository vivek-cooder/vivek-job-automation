"""Focused, pure offline regression tests for form adapter fixture boundaries and submission guards.

Tests:
1. _is_local_test_page() boundary validation:
   - Approved local fixture URLs are accepted (file:// and loopback HTTP/HTTPS with test_application.html).
   - External HTTPS pages are rejected.
   - External pages with path ending in /test_application.html are rejected.
   - URLs containing embedded userinfo/credentials are rejected.
   - UNC/network-share file URLs are rejected.
   - Unrelated local file paths are rejected.
   - Unrelated loopback paths are rejected.
   - .invalid TLD domains are rejected.
   - Unexpected URL schemes (javascript:, data:, ftp:) fail closed.
   - Malformed URLs fail closed.
   - Missing or unreadable page URLs fail closed.

2. prepare_application() submission guard validation:
   - Submit button click is blocked when auto_submit is False (default disabled).
   - Submit button click is blocked when auto_submit is omitted (default None/False).
   - Submit button click is blocked on external HTTPS pages even when auto_submit is True.
   - Submit button click is blocked on external fixture paths even when auto_submit is True.
   - Submit button click is blocked on UNC network file shares even when auto_submit is True.
   - Submit button click is blocked on URLs with embedded credentials even when auto_submit is True.
   - Submit button click is blocked on unrelated local files even when auto_submit is True.
   - Submit button click is blocked on .invalid domains even when auto_submit is True.
   - Submit button click is blocked when auto_submit is a truthy string ("true").
   - Submit button click is blocked when auto_submit is integer 1.
   - Submit button click is permitted ONLY on trusted local fixtures with explicit boolean True.

ZERO browser launches, ZERO Playwright imports, ZERO network or socket activity.
"""

import unittest
from unittest import mock

from worker.form_adapter import _is_local_test_page, prepare_application


class FakePage:
    """Lightweight fake Page object matching form adapter's inspection contract."""

    def __init__(self, url=None, raises_on_url=None):
        self._url = url
        self._raises_on_url = raises_on_url

    @property
    def url(self):
        if self._raises_on_url:
            raise self._raises_on_url
        return self._url

    def wait_for_timeout(self, timeout):
        pass

    def goto(self, url, **kwargs):
        pass


class TestLocalTestPageBoundaryOffline(unittest.TestCase):
    """Pure offline unit tests for _is_local_test_page() trusted fixture boundary."""

    # 1. Approved synthetic fixture URLs accepted
    def test_approved_file_fixture_url_accepted(self):
        """Approved local file fixture URL ending with test_application.html is accepted."""
        page = FakePage(url="file:///D:/project/JOB/vivek-job-automation/test_application.html")
        self.assertTrue(_is_local_test_page(page))

    def test_approved_localhost_http_fixture_url_accepted(self):
        """Approved HTTP localhost loopback URL with test_application.html is accepted."""
        page = FakePage(url="http://localhost:8000/test_application.html")
        self.assertTrue(_is_local_test_page(page))

    def test_approved_ipv4_loopback_http_fixture_url_accepted(self):
        """Approved HTTP 127.0.0.1 loopback URL with test_application.html is accepted."""
        page = FakePage(url="http://127.0.0.1:8000/test_application.html")
        self.assertTrue(_is_local_test_page(page))

    def test_approved_ipv6_loopback_http_fixture_url_accepted(self):
        """Approved HTTP [::1] IPv6 loopback URL with test_application.html is accepted."""
        page = FakePage(url="http://[::1]:8000/test_application.html")
        self.assertTrue(_is_local_test_page(page))

    def test_approved_localhost_https_fixture_url_accepted(self):
        """Approved HTTPS localhost loopback URL with test_application.html is accepted."""
        page = FakePage(url="https://localhost:8443/test_application.html")
        self.assertTrue(_is_local_test_page(page))

    # 2. Normal external HTTPS pages rejected
    def test_external_https_page_rejected(self):
        """Ordinary external career pages are rejected."""
        page = FakePage(url="https://jobs.example.com/apply")
        self.assertFalse(_is_local_test_page(page))

    def test_external_greenhouse_page_rejected(self):
        """External Greenhouse applicant tracking URL is rejected."""
        page = FakePage(url="https://boards.greenhouse.io/corp/jobs/12345")
        self.assertFalse(_is_local_test_page(page))

    # 3. External pages with path ending in /test_application.html rejected
    def test_external_page_with_fixture_path_rejected(self):
        """External domains hosting test_application.html must not receive fixture trust."""
        page = FakePage(url="https://malicious.example.com/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    def test_external_page_with_nested_fixture_path_rejected(self):
        """External domains with nested fixture paths are rejected."""
        page = FakePage(url="https://attacker.org/path/to/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    def test_deceptive_loopback_subdomain_rejected(self):
        """Deceptive hostnames like localhost.attacker.com must be rejected."""
        page = FakePage(url="http://localhost.attacker.com:8000/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    # 4. URLs containing username or password credentials rejected
    def test_embedded_credentials_on_loopback_rejected(self):
        """URLs containing username/password credentials on loopback are rejected."""
        page = FakePage(url="http://user:secret@127.0.0.1:8000/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    def test_embedded_username_only_on_localhost_rejected(self):
        """URLs containing embedded userinfo on localhost are rejected."""
        page = FakePage(url="http://admin@localhost:8000/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    # 5. UNC/network-share file URLs rejected
    def test_unc_network_share_file_url_rejected(self):
        """UNC-style remote network shares (file://hostname/...) must be rejected."""
        page = FakePage(url="file://remote-server/share/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    def test_unc_ip_address_network_share_rejected(self):
        """UNC-style network shares using IP addresses must be rejected."""
        page = FakePage(url="file://192.168.1.100/shared/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    # 6. Unrelated local file paths rejected
    def test_unrelated_local_file_path_rejected(self):
        """Arbitrary local HTML files outside test_application.html must be rejected."""
        page = FakePage(url="file:///C:/Users/User/Downloads/arbitrary_form.html")
        self.assertFalse(_is_local_test_page(page))

    def test_unrelated_project_file_rejected(self):
        """Other local HTML or JSON files in project root must be rejected."""
        page = FakePage(url="file:///D:/project/JOB/vivek-job-automation/other_page.html")
        self.assertFalse(_is_local_test_page(page))

    # 7. Unrelated loopback paths rejected
    def test_unrelated_loopback_path_rejected(self):
        """Non-fixture paths on localhost must be rejected."""
        page = FakePage(url="http://localhost:8000/arbitrary_page.html")
        self.assertFalse(_is_local_test_page(page))

    def test_unrelated_ipv4_loopback_path_rejected(self):
        """Non-fixture paths on 127.0.0.1 must be rejected."""
        page = FakePage(url="http://127.0.0.1:8000/admin/settings")
        self.assertFalse(_is_local_test_page(page))

    # 8. .invalid TLD domains rejected
    def test_invalid_tld_domain_rejected(self):
        """Domains ending in .invalid are not trusted local fixtures and must be rejected."""
        page = FakePage(url="https://example.invalid/jobs/1")
        self.assertFalse(_is_local_test_page(page))

    def test_invalid_tld_with_fixture_path_rejected(self):
        """.invalid domains even with test_application.html path must be rejected."""
        page = FakePage(url="http://test.invalid/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    # 9. Unexpected URL schemes and malformed URLs fail closed
    def test_javascript_scheme_fails_closed(self):
        """javascript: URI scheme fails closed."""
        page = FakePage(url="javascript:alert(1)")
        self.assertFalse(_is_local_test_page(page))

    def test_data_scheme_fails_closed(self):
        """data: URI scheme fails closed."""
        page = FakePage(url="data:text/html,<form><button>Submit</button></form>")
        self.assertFalse(_is_local_test_page(page))

    def test_ftp_scheme_fails_closed(self):
        """ftp: URI scheme fails closed."""
        page = FakePage(url="ftp://localhost/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    def test_malformed_url_fails_closed(self):
        """Malformed or unparseable URLs fail closed without crashing."""
        page = FakePage(url="http://[invalid-ipv6/test_application.html")
        self.assertFalse(_is_local_test_page(page))

    # 10. Missing or unreadable page URLs fail closed
    def test_none_page_url_fails_closed(self):
        """Missing page URL (None) fails closed."""
        page = FakePage(url=None)
        self.assertFalse(_is_local_test_page(page))

    def test_unreadable_page_url_exception_fails_closed(self):
        """Property access exception on page.url fails closed safely."""
        page = FakePage(raises_on_url=RuntimeError("Page crashed"))
        self.assertFalse(_is_local_test_page(page))


class TestSubmissionGuardBoundaryOffline(unittest.TestCase):
    """Pure offline unit tests for prepare_application() submission guard click prevention."""

    def _run_prepare_application(self, page, **kwargs):
        """Helper to execute prepare_application with all form inspection steps safely mocked."""
        fake_nav = mock.Mock()
        fake_nav.last_rejection = None
        applicant = {"name": "Test User", "email": "test@example.invalid"}
        mock_form = mock.Mock()
        mock_button = mock.Mock()

        with mock.patch("worker.form_adapter.install_page_guard"), \
             mock.patch("worker.form_adapter.current_page_is_authorized"), \
             mock.patch("worker.form_adapter.detect_verification", return_value=None), \
             mock.patch("worker.form_adapter.identify_application_form", return_value={"status": "Selected", "form": mock_form}), \
             mock.patch("worker.form_adapter.fill_application_form", return_value={
                 "status": "Filled",
                 "fields": [{"status": "filled", "field_type": "text"}],
                 "field_count": 1,
                 "needs_user_input": False,
                 "valid_for_submission": True,
                 "missing_fields": [],
             }), \
             mock.patch("worker.form_adapter._form_is_valid", return_value=True), \
             mock.patch("worker.form_adapter.find_submit_button", return_value=mock_button), \
             mock.patch("worker.form_adapter.detect_application_result", return_value={"status": "Submitted"}):

            result = prepare_application(
                page, applicant, navigation_policy=fake_nav, **kwargs
            )
            return result, mock_button

    # 1. Submission guard blocks clicking submit when auto_submit is disabled
    def test_submit_click_blocked_when_auto_submit_disabled(self):
        """When auto_submit is False, submit_button.click() is never called even on local fixture."""
        page = FakePage(url="http://127.0.0.1:8000/test_application.html")
        result, mock_button = self._run_prepare_application(page, auto_submit=False)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 2. Submission guard blocks clicking submit when auto_submit is omitted
    def test_submit_click_blocked_when_auto_submit_omitted(self):
        """When auto_submit argument is omitted, submission is blocked by default."""
        page = FakePage(url="http://127.0.0.1:8000/test_application.html")
        result, mock_button = self._run_prepare_application(page)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 3. Submission guard blocks clicking submit on external HTTPS pages even when auto_submit=True
    def test_submit_click_blocked_on_external_https_page_even_when_auto_submit_enabled(self):
        """On a real external HTTPS page, submit_button.click() is never called even if auto_submit=True."""
        page = FakePage(url="https://jobs.example.com/apply")
        result, mock_button = self._run_prepare_application(page, auto_submit=True)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))
        self.assertEqual(result.get("submission_blocked_reason"), "REAL_PAGE_SUBMISSION_DISABLED")

    # 4. Submission guard blocks clicking submit on external fixture path even when auto_submit=True
    def test_submit_click_blocked_on_external_fixture_path_even_when_auto_submit_enabled(self):
        """External page with /test_application.html path must never click submit even if auto_submit=True."""
        page = FakePage(url="https://malicious.example.com/test_application.html")
        result, mock_button = self._run_prepare_application(page, auto_submit=True)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))
        self.assertEqual(result.get("submission_blocked_reason"), "REAL_PAGE_SUBMISSION_DISABLED")

    # 5. Submission guard blocks clicking submit on UNC network share even when auto_submit=True
    def test_submit_click_blocked_on_unc_file_share_even_when_auto_submit_enabled(self):
        """UNC network file share must never click submit even if auto_submit=True."""
        page = FakePage(url="file://remote-server/share/test_application.html")
        result, mock_button = self._run_prepare_application(page, auto_submit=True)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 6. Submission guard blocks clicking submit on embedded credentials even when auto_submit=True
    def test_submit_click_blocked_on_embedded_credentials_even_when_auto_submit_enabled(self):
        """URL with embedded userinfo must never click submit even if auto_submit=True."""
        page = FakePage(url="http://user:secret@127.0.0.1:8000/test_application.html")
        result, mock_button = self._run_prepare_application(page, auto_submit=True)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 7. Submission guard blocks clicking submit on unrelated local files even when auto_submit=True
    def test_submit_click_blocked_on_unrelated_local_file_even_when_auto_submit_enabled(self):
        """Arbitrary local file outside test_application.html must never click submit even if auto_submit=True."""
        page = FakePage(url="file:///C:/Users/User/Downloads/arbitrary_form.html")
        result, mock_button = self._run_prepare_application(page, auto_submit=True)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 8. Submission guard blocks clicking submit on .invalid domains even when auto_submit=True
    def test_submit_click_blocked_on_invalid_tld_even_when_auto_submit_enabled(self):
        """Domains ending in .invalid must never click submit even if auto_submit=True."""
        page = FakePage(url="https://example.invalid/jobs/1")
        result, mock_button = self._run_prepare_application(page, auto_submit=True)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 9. Submission guard blocks clicking submit when auto_submit is a truthy string ("true")
    def test_submit_click_blocked_when_auto_submit_is_truthy_string(self):
        """Passing truthy string 'true' must never click submit even on approved local fixture."""
        page = FakePage(url="http://127.0.0.1:8000/test_application.html")
        result, mock_button = self._run_prepare_application(page, auto_submit="true")

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 10. Submission guard blocks clicking submit when auto_submit is integer 1
    def test_submit_click_blocked_when_auto_submit_is_integer_one(self):
        """Passing truthy integer 1 must never click submit even on approved local fixture."""
        page = FakePage(url="http://127.0.0.1:8000/test_application.html")
        result, mock_button = self._run_prepare_application(page, auto_submit=1)

        mock_button.click.assert_not_called()
        self.assertEqual(result.get("status"), "ReadyForSubmission")
        self.assertFalse(result.get("submission_enabled"))

    # 11. Submission guard permits submit click ONLY on approved local fixture with explicit boolean True
    def test_submit_click_permitted_only_on_trusted_fixture_with_explicit_true(self):
        """When on an approved local fixture AND auto_submit is explicitly True, click is permitted."""
        page = FakePage(url="http://127.0.0.1:8000/test_application.html")
        result, mock_button = self._run_prepare_application(page, auto_submit=True)

        mock_button.click.assert_called_once()
        self.assertEqual(result.get("status"), "Submitted")


if __name__ == "__main__":
    unittest.main()
