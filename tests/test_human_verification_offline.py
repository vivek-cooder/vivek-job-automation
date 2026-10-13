"""Focused, pure offline regression tests for human verification detection.

Tests:
- All supported challenge categories (CAPTCHA, OTP, Cloudflare, Email, Phone, Login approval).
- Supported widget and input selectors.
- Intended synthetic fixture clear state on local origins.
- The unscoped fixture bypass condition on external URLs (capturing intended safe behavior).
- Exception resilience when DOM or URL inspection fails.

ZERO browser launches, ZERO Playwright imports, ZERO network or socket activity.
"""

import unittest
from typing import Dict, Optional, Set

from worker.detector import (
    CAPTCHA_TERMS,
    CLOUDFLARE_TERMS,
    EMAIL_TERMS,
    LOGIN_APPROVAL_TERMS,
    OTP_TERMS,
    PHONE_TERMS,
    SECURITY_TERMS,
    detect_verification,
)


class FakeElement:
    """Lightweight fake DOM element exposing is_visible and is_enabled."""

    def __init__(self, visible: bool = True, enabled: bool = True) -> None:
        self._visible = visible
        self._enabled = enabled

    def is_visible(self) -> bool:
        return self._visible

    def is_enabled(self) -> bool:
        return self._enabled


class FakeLocator:
    """Lightweight fake Locator exposing count, first, is_visible, and inner_text."""

    def __init__(
        self,
        text: str = "",
        visible: bool = False,
        enabled: bool = False,
        count: int = 0,
        raises_on_text: Optional[Exception] = None,
        raises_on_visible: Optional[Exception] = None,
        raises_on_enabled: Optional[Exception] = None,
    ) -> None:
        self._text = text
        self._visible = visible
        self._enabled = enabled
        self._count = count
        self._raises_on_text = raises_on_text
        self._raises_on_visible = raises_on_visible
        self._raises_on_enabled = raises_on_enabled

    def inner_text(self, timeout: Optional[int] = None) -> str:
        if self._raises_on_text:
            raise self._raises_on_text
        return self._text

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> FakeElement:
        if self._raises_on_visible:
            raise self._raises_on_visible
        if self._raises_on_enabled:
            raise self._raises_on_enabled
        return FakeElement(visible=self._visible, enabled=self._enabled)

    def is_visible(self) -> bool:
        if self._raises_on_visible:
            raise self._raises_on_visible
        return self._visible

    def is_enabled(self) -> bool:
        if self._raises_on_enabled:
            raise self._raises_on_enabled
        return self._enabled


class FakePage:
    """Fake Page matching the detector's read-only DOM and URL contract."""

    def __init__(
        self,
        url: str = "https://example.invalid/jobs/1",
        body_text: str = "",
        visible_selectors: Optional[Set[str]] = None,
        enabled_selectors: Optional[Set[str]] = None,
        raises_on_url: Optional[Exception] = None,
        raises_on_body: Optional[Exception] = None,
        locator_exceptions: Optional[Dict[str, Exception]] = None,
    ) -> None:
        self._url = url
        self.body_text = body_text
        self.visible_selectors = set(visible_selectors or [])
        self.enabled_selectors = set(enabled_selectors or [])
        self._raises_on_url = raises_on_url
        self._raises_on_body = raises_on_body
        self._locator_exceptions = dict(locator_exceptions or {})

    @property
    def url(self) -> str:
        if self._raises_on_url:
            raise self._raises_on_url
        return self._url

    def locator(self, selector: str) -> FakeLocator:
        if selector in self._locator_exceptions:
            raise self._locator_exceptions[selector]

        if selector == "body":
            if self._raises_on_body:
                return FakeLocator(raises_on_text=self._raises_on_body)
            return FakeLocator(text=self.body_text, count=1)

        is_vis = selector in self.visible_selectors
        is_en = selector in self.enabled_selectors
        cnt = 1 if (is_vis or is_en) else 0
        return FakeLocator(visible=is_vis, enabled=is_en, count=cnt)


class TestCaptchaDetection(unittest.TestCase):
    """Test recognition of CAPTCHA terms and DOM widget selectors."""

    def test_all_defined_captcha_text_terms_detected(self):
        for term in CAPTCHA_TERMS:
            with self.subTest(term=term):
                page = FakePage(body_text=f"Please complete this: {term} before continuing.")
                self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_recaptcha_iframe_selector_detected(self):
        page = FakePage(visible_selectors={"iframe[src*='recaptcha']"})
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_recaptcha_title_iframe_selector_detected(self):
        page = FakePage(visible_selectors={"iframe[title*='reCAPTCHA']"})
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_g_recaptcha_container_selector_detected(self):
        page = FakePage(visible_selectors={".g-recaptcha"})
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_data_sitekey_attribute_selector_detected(self):
        page = FakePage(visible_selectors={"[data-sitekey]"})
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_hcaptcha_iframe_and_class_selectors_detected(self):
        for sel in ("iframe[src*='hcaptcha']", ".h-captcha"):
            with self.subTest(selector=sel):
                page = FakePage(visible_selectors={sel})
                self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")


class TestOtherVerificationCategories(unittest.TestCase):
    """Test OTP, Cloudflare, Login approval, Email, and Phone challenge recognition."""

    def test_all_defined_otp_text_terms_detected(self):
        for term in OTP_TERMS:
            with self.subTest(term=term):
                page = FakePage(body_text=f"Authentication required: {term} sent.")
                self.assertEqual(detect_verification(page), "OTP_REQUIRED")

    def test_otp_autocomplete_and_name_inputs_detected(self):
        """Under current detector.py, OTP input selectors alone with neutral body text
        do NOT trigger OTP_REQUIRED (returns None) because lines 80-82 require
        OTP_TERMS to be present in body text. This documents that input selectors
        cannot independently trigger OTP_REQUIRED under current code.
        """
        selectors = (
            "input[autocomplete='one-time-code']",
            "input[name*='otp' i]",
            "input[id*='otp' i]",
            "input[name*='verification' i]",
            "input[id*='verification' i]",
        )
        for sel in selectors:
            with self.subTest(selector=sel):
                page = FakePage(body_text="Standard job application form", visible_selectors={sel})
                self.assertIsNone(detect_verification(page))

    def test_cloudflare_challenge_url_pattern_detected(self):
        page = FakePage(url="https://employer.example/cdn-cgi/challenge-platform/h/b")
        self.assertEqual(detect_verification(page), "SECURITY_CHALLENGE_REQUIRED")

    def test_challenge_form_selector_detected_as_captcha(self):
        """#challenge-form is included in captcha_widget (detector.py:59) and returns CAPTCHA_REQUIRED."""
        page = FakePage(visible_selectors={"#challenge-form"})
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_cloudflare_phrases_detected(self):
        for term in CLOUDFLARE_TERMS:
            with self.subTest(term=term):
                page = FakePage(body_text=f"Protection: {term}")
                self.assertEqual(detect_verification(page), "SECURITY_CHALLENGE_REQUIRED")

    def test_cloudflare_keyword_with_ray_id_detected(self):
        page = FakePage(body_text="Cloudflare Ray ID: 87654321, verify connection")
        self.assertEqual(detect_verification(page), "SECURITY_CHALLENGE_REQUIRED")

    def test_login_approval_terms_detected(self):
        for term in LOGIN_APPROVAL_TERMS:
            with self.subTest(term=term):
                page = FakePage(body_text=f"Security gate: {term}")
                self.assertEqual(detect_verification(page), "LOGIN_APPROVAL_REQUIRED")

    def test_email_verification_terms_detected(self):
        for term in EMAIL_TERMS:
            with self.subTest(term=term):
                page = FakePage(body_text=f"Notice: {term}")
                self.assertEqual(detect_verification(page), "EMAIL_VERIFICATION_REQUIRED")

    def test_phone_verification_terms_detected(self):
        for term in PHONE_TERMS:
            with self.subTest(term=term):
                page = FakePage(body_text=f"Notice: {term}")
                self.assertEqual(detect_verification(page), "PHONE_VERIFICATION_REQUIRED")

    def test_generic_security_challenge_terms_detected(self):
        for term in SECURITY_TERMS:
            with self.subTest(term=term):
                page = FakePage(body_text=f"Alert: {term}")
                self.assertEqual(detect_verification(page), "SECURITY_CHALLENGE_REQUIRED")


class TestFixtureBypassRegression(unittest.TestCase):
    """Test the #verificationSuccess and #submitButton fixture exception."""

    def test_local_synthetic_fixture_clear_state_returns_none(self):
        """Intended behavior: on local fixture, visible success and enabled submit clears gate."""
        page = FakePage(
            url="http://127.0.0.1:8000/test_application.html",
            body_text="Human Verification complete. Submit application.",
            visible_selectors={"#verificationSuccess"},
            enabled_selectors={"#submitButton"},
        )
        self.assertIsNone(detect_verification(page))

    def test_local_synthetic_fixture_with_file_scheme_returns_none(self):
        """Intended behavior: on file:// fixture, visible success and enabled submit clears gate."""
        page = FakePage(
            url="file:///C:/project/test_application.html",
            body_text="Human Verification complete.",
            visible_selectors={"#verificationSuccess"},
            enabled_selectors={"#submitButton"},
        )
        self.assertIsNone(detect_verification(page))

    def test_hidden_verification_success_does_not_clear_gate(self):
        """When #verificationSuccess is hidden, active verification instructions must be detected."""
        page = FakePage(
            url="http://127.0.0.1:8000/test_application.html",
            body_text="Human Verification. Please verify you are human.",
            visible_selectors=set(),  # #verificationSuccess is hidden
            enabled_selectors={"#submitButton"},
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_disabled_submit_button_does_not_clear_gate(self):
        """When #submitButton is disabled, active verification instructions must be detected."""
        page = FakePage(
            url="http://127.0.0.1:8000/test_application.html",
            body_text="Human Verification. Please verify you are human.",
            visible_selectors={"#verificationSuccess"},
            enabled_selectors=set(),  # #submitButton is disabled
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_external_page_with_captcha_and_fixture_selectors_does_not_bypass(self):
        """INTENDED SAFE BEHAVIOR (EXPECTED PRE-FIX FAILURE):

        An external page (https://employer.example/apply) containing an active CAPTCHA
        must NOT bypass verification checks simply because elements named
        #verificationSuccess and #submitButton happen to be visible/enabled.

        Under current pre-fix detector.py, this will return None because lines 53-54
        lack origin scoping. The intended safe behavior asserts CAPTCHA_REQUIRED.
        """
        page = FakePage(
            url="https://employer.example/apply",
            body_text="Please complete the security check to continue.",
            visible_selectors={"#verificationSuccess", "iframe[src*='recaptcha']"},
            enabled_selectors={"#submitButton"},
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_unc_network_file_url_denied_fixture_exemption(self):
        """UNC-style network file:// URLs must be rejected and must not bypass verification."""
        page = FakePage(
            url="file://remote-server/share/test_application.html",
            body_text="Please complete the security check to continue.",
            visible_selectors={"#verificationSuccess", "iframe[src*='recaptcha']"},
            enabled_selectors={"#submitButton"},
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_arbitrary_local_html_file_denied_fixture_exemption(self):
        """Arbitrary local HTML files outside test_application.html must not bypass verification."""
        page = FakePage(
            url="file:///C:/Users/User/Downloads/arbitrary_form.html",
            body_text="Please complete the security check to continue.",
            visible_selectors={"#verificationSuccess", "iframe[src*='recaptcha']"},
            enabled_selectors={"#submitButton"},
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_embedded_credentials_url_denied_fixture_exemption(self):
        """URLs containing embedded userinfo/credentials must be rejected."""
        page = FakePage(
            url="http://user:secret@127.0.0.1:8000/test_application.html",
            body_text="Please complete the security check to continue.",
            visible_selectors={"#verificationSuccess", "iframe[src*='recaptcha']"},
            enabled_selectors={"#submitButton"},
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_deceptive_loopback_hostname_denied_fixture_exemption(self):
        """Deceptive hostnames like localhost.attacker.example must be rejected."""
        page = FakePage(
            url="http://localhost.attacker.example:8000/test_application.html",
            body_text="Please complete the security check to continue.",
            visible_selectors={"#verificationSuccess", "iframe[src*='recaptcha']"},
            enabled_selectors={"#submitButton"},
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_non_fixture_loopback_path_denied_fixture_exemption(self):
        """Non-fixture paths on loopback must be rejected."""
        page = FakePage(
            url="http://127.0.0.1:8000/arbitrary_page.html",
            body_text="Please complete the security check to continue.",
            visible_selectors={"#verificationSuccess", "iframe[src*='recaptcha']"},
            enabled_selectors={"#submitButton"},
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")

    def test_ipv6_loopback_fixture_clear_state_returns_none(self):
        """Legitimate IPv6 loopback [::1] fixture path clears gate when selectors match."""
        page = FakePage(
            url="http://[::1]:8000/test_application.html",
            body_text="Human Verification complete. Submit application.",
            visible_selectors={"#verificationSuccess"},
            enabled_selectors={"#submitButton"},
        )
        self.assertIsNone(detect_verification(page))

    def test_malformed_url_fails_closed_and_detects_captcha(self):
        """Malformed or unparseable URLs fail closed and do not bypass verification."""
        page = FakePage(
            body_text="Please complete the security check to continue.",
            visible_selectors={"#verificationSuccess", "iframe[src*='recaptcha']"},
            enabled_selectors={"#submitButton"},
            raises_on_url=ValueError("Invalid IPv6 URL format"),
        )
        self.assertEqual(detect_verification(page), "CAPTCHA_REQUIRED")


class TestDetectorExceptionHandling(unittest.TestCase):
    """Test graceful and safe handling when DOM or URL inspection fails."""

    def test_body_text_locator_timeout_or_error_fails_safe(self):
        """When page body cannot be read, detector should not crash."""
        page = FakePage(
            raises_on_body=RuntimeError("Locator timeout 1500ms exceeded"),
        )
        self.assertIsNone(detect_verification(page))

    def test_page_url_inspection_error_fails_safe(self):
        """When page.url raises an error, detector should not crash."""
        page = FakePage(
            body_text="Normal job application form text",
            raises_on_url=RuntimeError("Target page crashed"),
        )
        self.assertIsNone(detect_verification(page))

    def test_widget_locator_error_fails_safe(self):
        """When widget selector lookup throws, detector should not crash."""
        page = FakePage(
            body_text="Normal job text",
            locator_exceptions={"#challenge-form": RuntimeError("Protocol error")},
        )
        self.assertIsNone(detect_verification(page))

    def test_clean_page_returns_none(self):
        """A page without verification terms or challenge selectors returns None."""
        page = FakePage(
            url="https://employer.example/careers/frontend-developer",
            body_text="We are hiring a Senior Frontend Engineer. Apply below with your resume and contact information.",
        )
        self.assertIsNone(detect_verification(page))


if __name__ == "__main__":
    unittest.main()
