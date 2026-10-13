"""Fail-closed URL authorization and Playwright request interception."""

import json
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class NavigationRejected(ValueError):
    def __init__(self, reason, url):
        super().__init__(reason)
        self.reason = reason
        self.url = url


def normalize_hostname(value):
    """Return a canonical DNS hostname; never performs suffix/substring matching."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("HOSTNAME_REQUIRED")
    value = value.strip().rstrip(".").casefold()
    try:
        return ipaddress.ip_address(value).compressed.casefold()
    except ValueError:
        pass
    try:
        host = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("INVALID_HOSTNAME") from exc
    if len(host) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise ValueError("INVALID_HOSTNAME")
    return host


class NavigationPolicy:
    """Explicit hostname policy. WWW aliases require their own allowlist entry."""

    def __init__(self, allowed_hosts=(), *, allow_local_files=False):
        if isinstance(allowed_hosts, (str, bytes)):
            raise ValueError("allowed_hosts must be a list of hostnames")
        self.allowed_hosts = frozenset(normalize_hostname(host) for host in allowed_hosts)
        self.allow_local_files = bool(allow_local_files)
        self.last_rejection = None
        self._installed_contexts = []

    @classmethod
    def from_config(cls, path=CONFIG_PATH):
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                config = json.load(handle)
            navigation = config.get("navigation", {})
            if not isinstance(navigation, dict):
                raise ValueError("navigation configuration must be an object")
            hosts = navigation.get("allowed_hosts", [])
            if not isinstance(hosts, list):
                raise ValueError("navigation.allowed_hosts must be a list")
            return cls(hosts)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("NAVIGATION_POLICY_INVALID") from exc

    @classmethod
    def for_local_tests(cls, *, allowed_hosts=("127.0.0.1", "localhost", "::1"), allow_local_files=True):
        """Narrow test-only policy; never loaded by the production worker."""
        return cls(allowed_hosts, allow_local_files=allow_local_files)

    def check(self, url):
        """Raise NavigationRejected unless the URL is explicitly authorized."""
        try:
            parts = urlsplit(str(url))
            if parts.username is not None or parts.password is not None:
                raise NavigationRejected("UNSUPPORTED_NAVIGATION_URL", str(url))
            if parts.scheme.casefold() == "file":
                if self.allow_local_files and not parts.netloc and parts.path:
                    return True
                raise NavigationRejected("UNAUTHORIZED_HOST", str(url))
            if parts.scheme.casefold() != "https" and not (
                parts.scheme.casefold() == "http"
                and self.allow_local_files
                and normalize_hostname(parts.hostname or "") in self.allowed_hosts
            ):
                raise NavigationRejected("UNSUPPORTED_NAVIGATION_URL", str(url))
            hostname = normalize_hostname(parts.hostname or "")
        except NavigationRejected:
            raise
        except (ValueError, UnicodeError):
            raise NavigationRejected("UNSUPPORTED_NAVIGATION_URL", str(url))
        if hostname not in self.allowed_hosts:
            raise NavigationRejected("UNAUTHORIZED_HOST", str(url))
        return True

    def install(self, context):
        """Abort every unauthorized HTTP(S) request, including redirect hops."""
        for installed_context, handler in self._installed_contexts:
            if installed_context is context:
                return handler

        def guard(route):
            try:
                self.check(route.request.url)
            except NavigationRejected as exc:
                self.last_rejection = exc.reason
                route.abort("blockedbyclient")
                return
            route.continue_()

        context.route("**/*", guard)
        self._installed_contexts.append((context, guard))
        return guard

    def clear_rejection(self):
        self.last_rejection = None


def configured_policy():
    return NavigationPolicy.from_config()


def current_page_is_authorized(page, policy):
    """Fail closed for pages reached through redirects or user-driven navigation."""
    try:
        return policy.check(str(page.url))
    except NavigationRejected as exc:
        raise exc
    except Exception as exc:
        raise NavigationRejected("UNSUPPORTED_NAVIGATION_URL", str(getattr(page, "url", ""))) from exc


def install_page_guard(page, policy):
    """Install the policy on a Playwright page's context when one is available."""
    context = getattr(page, "context", None)
    if context is None:
        return False  # Non-browser mock objects used by unit tests.
    policy.install(context)
    return True
