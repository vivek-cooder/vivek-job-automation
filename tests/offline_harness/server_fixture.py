"""Local mock server fixture for isolated offline browser testing.

Defines an inert context manager fixture that binds strictly to 127.0.0.1 on an
operating-system-assigned ephemeral port (port 0) when entered. Serves only
synthetic test fixtures from mock_pages.py.

IMPORTANT:
- This module is inert upon import. It does NOT start a server or open sockets.
- Do NOT run, instantiate, or execute this module during Phase 22.
- Binding to any non-loopback address (e.g., 0.0.0.0, LAN, public IP) is strictly forbidden.
- Zero network proxying, forwarding, or external connections are allowed.
- Request bodies, headers, and query parameters are discarded without persistence.
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse
from typing import Optional

from tests.offline_harness.mock_pages import MOCK_PAGES

# Absolute local loopback constraint
LOCAL_LOOPBACK_HOST: str = "127.0.0.1"


class _MockHarnessHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Minimal, privacy-preserving HTTP request handler for synthetic test fixtures."""

    # HTTP protocol version
    protocol_version = "HTTP/1.1"
    timeout = 2.0  # Conservative socket read timeout in seconds

    def log_message(self, format: str, *args) -> None:
        """Suppress default logging to prevent leaking query parameters or URLs to disk/console."""
        pass

    def _sanitize_and_resolve_path(self) -> Optional[str]:
        """Parse request path, reject proxy attempts, and return clean path."""
        # Extract the raw request-target token defensively from requestline
        raw_target = ""
        if hasattr(self, "requestline") and isinstance(self.requestline, str):
            parts = self.requestline.split()
            if len(parts) >= 2:
                raw_target = parts[1]

        # Reject forward proxy requests (e.g., http://, https://, or //)
        if (
            self.path.startswith("http://")
            or self.path.startswith("https://")
            or self.path.startswith("//")
            or raw_target.startswith("http://")
            or raw_target.startswith("https://")
            or raw_target.startswith("//")
        ):
            self.send_error(400, "Proxy requests are strictly forbidden")
            return None

        target_to_split = raw_target if raw_target else self.path
        parsed = urllib.parse.urlsplit(target_to_split)

        # Reject targets with explicit authority or scheme as proxy attempts
        if parsed.netloc or parsed.scheme:
            self.send_error(400, "Proxy requests are strictly forbidden")
            return None

        # Fallback parse on self.path if raw_target did not yield a path
        if not parsed.path and self.path:
            parsed = urllib.parse.urlsplit(self.path)

        return parsed.path

    def do_HEAD(self) -> None:
        """Handle HEAD requests for synthetic routes."""
        clean_path = self._sanitize_and_resolve_path()
        if clean_path is None:
            return

        if clean_path in MOCK_PAGES:
            content = MOCK_PAGES[clean_path]
            content_bytes = content.encode("utf-8")
            content_type = "text/plain; charset=utf-8" if clean_path == "/robots.txt" else "text/html; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content_bytes)))
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()

    def do_GET(self) -> None:
        """Handle GET requests by serving synthetic fixtures from MOCK_PAGES."""
        clean_path = self._sanitize_and_resolve_path()
        if clean_path is None:
            return

        if clean_path in MOCK_PAGES:
            content = MOCK_PAGES[clean_path]
            content_bytes = content.encode("utf-8")
            content_type = "text/plain; charset=utf-8" if clean_path == "/robots.txt" else "text/html; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content_bytes)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content_bytes)
        else:
            not_found_msg = b"404 Not Found - Offline Test Harness Fixture"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(not_found_msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(not_found_msg)

    def do_POST(self) -> None:
        """Handle synthetic form submissions without storing or inspecting sensitive payload."""
        clean_path = self._sanitize_and_resolve_path()
        if clean_path is None:
            return

        # Drain request body up to content-length to avoid broken pipe, but DO NOT store or log it
        content_length_header = self.headers.get("Content-Length")
        if content_length_header:
            try:
                length = int(content_length_header)
                if 0 < length <= 1048576:  # Max 1MB drain
                    if hasattr(self.connection, "settimeout"):
                        self.connection.settimeout(2.0)
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass

        if clean_path == "/apply" or clean_path == "/apply-success":
            success_content = MOCK_PAGES["/apply-success"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(success_content)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(success_content)
        else:
            not_found_msg = b"404 Not Found - Unsupported Test POST Path"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(not_found_msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(not_found_msg)


class LocalMockServerFixture:
    """Context manager for running a local-only synthetic mock server on an ephemeral port.

    Must be explicitly entered via `with LocalMockServerFixture() as server:` in authorized future tests.
    Importing or instantiating this class DOES NOT start the server.
    """

    def __init__(self, bind_host: str = LOCAL_LOOPBACK_HOST) -> None:
        if bind_host != LOCAL_LOOPBACK_HOST:
            raise ValueError(
                f"Security violation: LocalMockServerFixture may only bind to '{LOCAL_LOOPBACK_HOST}', "
                f"attempted '{bind_host}'."
            )
        self._bind_host: str = bind_host
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._port: Optional[int] = None

    @property
    def port(self) -> int:
        """Return operating-system assigned ephemeral port. Raises RuntimeError if not running."""
        if self._port is None:
            raise RuntimeError("Server is not currently running.")
        return self._port

    @property
    def base_url(self) -> str:
        """Return base URL for the running local server (e.g. 'http://127.0.0.1:54321')."""
        return f"http://{self._bind_host}:{self.port}"

    def get_url(self, path: str) -> str:
        """Return full mock URL for a given path."""
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self.base_url}{normalized_path}"

    def __enter__(self) -> LocalMockServerFixture:
        """Start the local mock server on 127.0.0.1 with ephemeral port (0)."""
        # Port 0 instructs the operating system kernel to allocate an unused ephemeral port
        self._server = http.server.ThreadingHTTPServer(
            (self._bind_host, 0),
            _MockHarnessHTTPHandler,
        )
        self._server.timeout = 2.0
        self._port = self._server.server_address[1]

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="OfflineHarnessServerThread",
            daemon=True,
        )
        self._server_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Cleanly terminate server and release socket."""
        if self._server is not None:
            try:
                self._server.shutdown()
            finally:
                self._server.server_close()
                self._server = None

        if self._server_thread is not None:
            self._server_thread.join(timeout=3.0)
            self._server_thread = None

        self._port = None
