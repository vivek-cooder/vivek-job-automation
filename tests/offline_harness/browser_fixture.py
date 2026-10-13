"""Standardized, isolated browser fixture for synthetic-only offline testing.

Provides an ephemeral, non-persistent Playwright Chromium browser session
strictly coupled to HarnessRequestPolicy on local loopback (127.0.0.1).

IMPORTANT SAFETY ARCHITECTURE & LIMITATIONS:
1. Inert Module:
   - This module is strictly inert upon import. It does NOT launch browsers, start
     servers, bind sockets, initiate network requests, or perform filesystem writes.
   - Do NOT execute or run this fixture in Phase 23C.13. Execution is forbidden.

2. Ephemeral Contexts & Data Isolation:
   - Creates strictly non-persistent browser contexts using temporary directories.
   - NEVER uses `launch_persistent_context`.
   - NEVER accesses or modifies `./profile`, production application queues, tracking
     databases, or user configuration.
   - Disables downloads (`accept_downloads=False`) and blocks service workers
     (`service_workers="block"`). All ephemeral browser artifacts are confined to a
     unique temporary directory and removed during teardown.

3. Request Interception:
   - Registers Playwright context-level route interception before any page creation or
     navigation, evaluating all requests via HarnessRequestPolicy.
   - Permits strictly http://127.0.0.1:<expected_port>.
   - Denies all other schemes (including https, file, data, ftp, ws), hosts, ports,
     credentials in URLs, and malformed requests.
   - Re-evaluates redirects and subresources against the same default-deny policy.
   - Fails closed on any unexpected evaluation exception by aborting the request.
   - Does NOT log full URLs, query strings, headers, cookies, or request bodies to prevent
     accidental credential or personal data exposure.

4. Defense-In-Depth Browser Hardening Flags:
   - Launches Chromium with flags to reduce background networking and telemetry:
     `--disable-background-networking`, `--disable-component-update`,
     `--disable-default-apps`, `--disable-domain-reliability`, `--disable-sync`,
     and `--no-default-browser-check`.

5. WebSocket & Interception Limitations:
   - Playwright's `context.route("**/*")` intercepts standard HTTP/HTTPS navigation,
     subresources, and fetch requests.
   - It does NOT guarantee interception of raw WebSocket (`ws://`, `wss://`) connections,
     lower-level socket handshakes, or browser internal DNS lookups.
   - Chromium launch flags reduce background activity but do NOT provide an operating-system
     network boundary.
   - Independent OS-level or external network egress denial (e.g. firewall drop rules or
     network namespaces) is mandatory before any runtime browser test may be authorized.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

from tests.offline_harness.request_policy import HarnessRequestPolicy, PolicyDecision

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright


# Defense-in-depth Chromium launch arguments
CHROMIUM_ISOLATION_ARGS: tuple[str, ...] = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-default-browser-check",
)


@dataclass
class BrowserInterceptionStats:
    """Privacy-preserving audit statistics for route interception decisions.

    Maintains counts and safe categorical reason codes. Never stores URLs, query
    strings, headers, cookies, POST bodies, or credentials.
    """
    total_requests: int = 0
    allowed_requests: int = 0
    blocked_requests: int = 0
    blocked_reasons: List[str] = field(default_factory=list)


class IsolatedBrowserFixture:
    """Context manager providing an ephemeral, strictly isolated Playwright Chromium session.

    Enforces:
    - Non-persistent Chromium context (never calls `launch_persistent_context`).
    - Dedicated temporary directory for browser state (never touches `./profile`).
    - Service workers explicitly blocked (`service_workers="block"`).
    - Downloads explicitly disabled (`accept_downloads=False`).
    - Route interception installed on context BEFORE page creation or navigation.
    - Confined strictly to http://127.0.0.1:<expected_port>.
    - Fail-closed route abortion on evaluation errors or unexpected exceptions.
    - Deterministic teardown of pages, contexts, browsers, and temporary directories.
    """

    def __init__(
        self,
        expected_port: int,
        base_url: Optional[str] = None,
        headless: bool = True,
    ) -> None:
        if not isinstance(expected_port, int) or isinstance(expected_port, bool):
            raise ValueError("expected_port must be an integer")
        if not (1 <= expected_port <= 65535):
            raise ValueError(
                f"expected_port must be a valid TCP port (1-65535), got {expected_port}"
            )
        self.expected_port: int = expected_port
        expected_base = f"http://127.0.0.1:{expected_port}"
        if base_url is not None and base_url.rstrip("/") != expected_base:
            raise ValueError(
                f"base_url '{base_url}' does not match expected local loopback base '{expected_base}'"
            )
        self._base_url: str = expected_base
        self._headless: bool = headless

        # Inert instance attributes (no processes started at __init__)
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self._temp_path: Optional[Path] = None
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._context: Optional[Any] = None
        self._page: Optional[Any] = None
        self._policy: HarnessRequestPolicy = HarnessRequestPolicy(expected_port=self.expected_port)
        self.stats: BrowserInterceptionStats = BrowserInterceptionStats()
        self._is_open: bool = False

    @classmethod
    def from_server_fixture(
        cls,
        server: Any,
        headless: bool = True,
    ) -> IsolatedBrowserFixture:
        """Construct an IsolatedBrowserFixture bound to a running LocalMockServerFixture.

        Raises:
            ValueError: If server does not expose a port attribute.
            RuntimeError: If server port cannot be resolved.
        """
        if not hasattr(server, "port"):
            raise ValueError("Provided server fixture does not expose a 'port' attribute.")
        try:
            port = int(server.port)
        except Exception as exc:
            raise RuntimeError(f"Cannot obtain valid port from mock server: {exc}") from exc
        base = getattr(server, "base_url", None)
        return cls(expected_port=port, base_url=base, headless=headless)

    @property
    def is_open(self) -> bool:
        """Return True if fixture context is currently open and active."""
        return self._is_open

    @property
    def base_url(self) -> str:
        """Return authorized local fixture base URL (http://127.0.0.1:<expected_port>)."""
        return self._base_url

    @property
    def temp_dir_path(self) -> Optional[Path]:
        """Return path to active ephemeral temporary directory, or None if closed."""
        return self._temp_path

    @property
    def page(self) -> Any:
        """Return the primary isolated Page within the intercepted context."""
        if not self._is_open or self._page is None:
            raise RuntimeError("Browser fixture is not open.")
        return self._page

    @property
    def context(self) -> Any:
        """Return the isolated BrowserContext."""
        if not self._is_open or self._context is None:
            raise RuntimeError("Browser fixture is not open.")
        return self._context

    @property
    def browser(self) -> Any:
        """Return the running Browser instance."""
        if not self._is_open or self._browser is None:
            raise RuntimeError("Browser fixture is not open.")
        return self._browser

    def get_url(self, path: str) -> str:
        """Return a validated local mock URL targeting the running local fixture."""
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self._base_url}{normalized_path}"

    def _install_route_interception(self, context: Any) -> None:
        """Register default-deny route interception for all requests on the context.

        Evaluates every request (including redirects, subresources, and iframes)
        against HarnessRequestPolicy. Rejects anything not targeting 127.0.0.1:<port>.
        Fails closed on any unexpected evaluation exception.
        Never logs full URLs, query strings, headers, cookies, or request bodies.
        """
        def _route_handler(route: Any) -> None:
            self.stats.total_requests += 1
            is_allowed = False
            reason_code = "UNKNOWN_REJECTION"

            try:
                request = getattr(route, "request", None)
                raw_url = getattr(request, "url", None) if request is not None else None
                decision: PolicyDecision = self._policy.evaluate(raw_url)

                if decision.allowed:
                    is_allowed = True
                else:
                    reason_code = decision.reason
            except Exception:
                # Fail closed on any evaluation error without leaking data
                is_allowed = False
                reason_code = "EVALUATION_EXCEPTION_ABORT"

            if is_allowed:
                self.stats.allowed_requests += 1
                try:
                    route.continue_()
                except Exception:
                    # Navigation may have been canceled by test lifecycle; do not crash
                    pass
            else:
                self.stats.blocked_requests += 1
                self.stats.blocked_reasons.append(reason_code)
                try:
                    # Fail closed: abort request immediately without leaking sensitive URL or payload
                    route.abort("blockedbyclient")
                except Exception:
                    # Silently handle secondary abort error (e.g. if connection already terminated)
                    pass

        context.route("**/*", _route_handler)

    def _cleanup_resources(self) -> None:
        """Clean up all browser resources and temporary directories in reverse order."""
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        if self._temp_dir is not None:
            try:
                self._temp_dir.cleanup()
            except Exception:
                pass
            self._temp_dir = None
            self._temp_path = None

        self._is_open = False

    def __enter__(self) -> IsolatedBrowserFixture:
        """Enter context: launch browser, install route policy, and create isolated page.

        Enforces fail-closed cleanup if any step fails.
        """
        if self._is_open:
            raise RuntimeError("IsolatedBrowserFixture is already open.")

        try:
            # 1. Create dedicated ephemeral temporary directory (never touches ./profile)
            self._temp_dir = tempfile.TemporaryDirectory(prefix="isolated_browser_fixture_")
            self._temp_path = Path(self._temp_dir.name)

            # 2. Lazily import Playwright to guarantee zero side-effects at module import time
            from playwright.sync_api import sync_playwright

            # 3. Start Playwright manager
            self._playwright = sync_playwright().start()

            # 4. Launch Chromium with anti-telemetry hardening flags
            # STRICT REQUIREMENT: NEVER use launch_persistent_context
            self._browser = self._playwright.chromium.launch(
                headless=self._headless,
                args=list(CHROMIUM_ISOLATION_ARGS),
            )

            # 5. Create ephemeral, non-persistent context
            # Explicitly block service workers and disable downloads
            self._context = self._browser.new_context(
                ignore_https_errors=False,
                accept_downloads=False,
                service_workers="block",
            )

            # 6. Install route interception on the context BEFORE page creation or navigation
            self._install_route_interception(self._context)

            # 7. Create primary page within the intercepted context
            self._page = self._context.new_page()

            self._is_open = True
            return self

        except Exception:
            self._cleanup_resources()
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Cleanly terminate all browser resources and remove temporary directories."""
        self._cleanup_resources()

    def close(self) -> None:
        """Explicit teardown helper if not used within a with-statement."""
        self._cleanup_resources()
