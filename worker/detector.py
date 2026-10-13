"""Human-verification and application-result signals; never solves challenges."""

from urllib.parse import urlsplit

CAPTCHA_TERMS = (
    "captcha", "recaptcha", "reCAPTCHA", "hcaptcha", "h-captcha",
    "verify you are human", "human verification", "complete the security check",
)
OTP_TERMS = (
    "one-time password", "one time password", "verification code",
    "enter the code", "otp", "security code",
)
EMAIL_TERMS = ("verify your email", "email verification", "confirm your email")
PHONE_TERMS = ("verify your phone", "phone verification", "confirm your phone")
SECURITY_TERMS = ("security challenge", "unusual activity", "additional verification")
CLOUDFLARE_TERMS = ("checking your browser", "just a moment", "enable javascript and cookies", "attention required")
LOGIN_APPROVAL_TERMS = ("approve sign-in", "approve sign in", "check your authenticator", "approve the login request")


def _body_text(page):
    try:
        return page.locator("body").inner_text(timeout=1500).casefold()
    except Exception:
        return ""


def _visible(page, selector):
    try:
        loc = page.locator(selector)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _enabled(page, selector):
    try:
        loc = page.locator(selector)
        return loc.count() > 0 and loc.first.is_enabled()
    except Exception:
        return False


def _is_local_fixture_page(page):
    """Return True only if page URL is on an explicitly trusted local fixture."""
    try:
        raw = getattr(page, "url", None)
        if raw is None:
            return False
        parts = urlsplit(str(raw))
        # Disallow embedded userinfo / credentials
        if parts.username is not None or parts.password is not None:
            return False
        scheme = (parts.scheme or "").casefold()
        host = (parts.hostname or "").casefold()
        path = (parts.path or "").replace("\\", "/").casefold()

        # Restrict to the known test application fixture path
        if not (path.endswith("/test_application.html") or path == "test_application.html"):
            return False

        # file:// must be purely local (empty authority/netloc; rejects UNC network shares)
        if scheme == "file":
            return not parts.netloc

        # HTTP/HTTPS must be exact loopback origin
        if scheme in {"http", "https"}:
            return host in {"localhost", "127.0.0.1", "::1"}

        return False
    except Exception:
        return False


def detect_verification(page):
    """Return a reason code for an active human gate, or None when clear."""
    text = _body_text(page)
    try:
        url = str(page.url).casefold()
    except Exception:
        url = ""

    # The local fixture retains its verification instructions after verification.
    # Its enabled submit button plus visible success marker is an explicit clear state,
    # but only on demonstrably trusted local test fixtures.
    if _is_local_fixture_page(page) and _visible(page, "#verificationSuccess") and _enabled(page, "#submitButton"):
        return None

    captcha_widget = any(_visible(page, selector) for selector in (
        "iframe[src*='recaptcha']", "iframe[title*='reCAPTCHA']",
        ".g-recaptcha", "[data-sitekey]", "iframe[src*='hcaptcha']",
        ".h-captcha", "#challenge-form",
    ))
    if captcha_widget or any(term.casefold() in text for term in CAPTCHA_TERMS):
        return "CAPTCHA_REQUIRED"
    if "cdn-cgi/challenge" in url or _visible(page, "#challenge-form") or any(term in text for term in CLOUDFLARE_TERMS):
        return "SECURITY_CHALLENGE_REQUIRED"
    if any(term in text for term in LOGIN_APPROVAL_TERMS):
        return "LOGIN_APPROVAL_REQUIRED"
    if any(term in text for term in EMAIL_TERMS):
        return "EMAIL_VERIFICATION_REQUIRED"
    if any(term in text for term in PHONE_TERMS):
        return "PHONE_VERIFICATION_REQUIRED"
    if "cloudflare" in text and any(term in text for term in ("challenge", "checking", "verify", "ray id")):
        return "SECURITY_CHALLENGE_REQUIRED"
    if any(term in text for term in SECURITY_TERMS):
        return "SECURITY_CHALLENGE_REQUIRED"
    code_input = any(_visible(page, selector) for selector in (
        "input[autocomplete='one-time-code']", "input[name*='otp' i]",
        "input[id*='otp' i]", "input[name*='verification' i]",
        "input[id*='verification' i]",
    ))
    if code_input and any(term.casefold() in text for term in OTP_TERMS):
        return "OTP_REQUIRED"
    if any(term.casefold() in text for term in OTP_TERMS):
        return "OTP_REQUIRED"
    return None


def detect_application_result(page):
    """Conservative success/failure result recognition; ambiguous states return None."""
    try:
        url = str(page.url)
    except Exception:
        url = ""
    path = urlsplit(url).path.casefold()
    text = _body_text(page)
    if _visible(page, "#submitSuccess"):
        return {"status": "Submitted", "signal": "success_selector:#submitSuccess"}
    if any(term in path for term in ("application-confirmation", "application-submitted", "thank-you")):
        return {"status": "Submitted", "signal": "confirmation_url"}
    success_phrases = (
        "application received", "application has been received",
        "application submitted", "application has been submitted",
        "thank you for applying", "thank you for your application",
        "we have received your application", "your application was sent",
    )
    if any(term in text for term in success_phrases):
        return {"status": "Submitted", "signal": "confirmation_text"}
    failure_phrases = (
        "unable to submit your application", "application submission failed",
        "there was an error submitting", "could not submit your application",
    )
    if any(term in text for term in failure_phrases):
        return {"status": "Failed", "signal": "submission_error_text"}
    return None
