"""Local loopback integration tests for LocalMockServerFixture.

Validates mock server lifecycle, loopback binding, ephemeral port allocation,
synthetic GET/POST routing, and proxy request rejection.
Strictly communicates over 127.0.0.1 only. Zero external network access, zero browser interaction.
"""

import http.client
import socket
import unittest

from tests.offline_harness.server_fixture import (
    LOCAL_LOOPBACK_HOST,
    LocalMockServerFixture,
)


class LocalMockServerFixtureTests(unittest.TestCase):
    """Test suite for the offline mock server fixture on local loopback."""

    def test_lifecycle_and_binding(self) -> None:
        """1. Verify server lifecycle, 127.0.0.1 binding, ephemeral port, and clean shutdown."""
        fixture = LocalMockServerFixture()

        # Before entering context, server is not running
        with self.assertRaises(RuntimeError):
            _ = fixture.port

        with fixture as server:
            # Verify reported host is exactly loopback 127.0.0.1
            self.assertEqual(server._bind_host, LOCAL_LOOPBACK_HOST)
            self.assertEqual(server._bind_host, "127.0.0.1")

            # Verify assigned port is in valid TCP ephemeral range
            port = server.port
            self.assertIsInstance(port, int)
            self.assertGreater(port, 1024)
            self.assertLessEqual(port, 65535)

            # Verify base_url uses loopback host and assigned port
            expected_base_url = f"http://127.0.0.1:{port}"
            self.assertEqual(server.base_url, expected_base_url)
            self.assertEqual(server.get_url("/job"), f"{expected_base_url}/job")

            # Verify server thread is running
            self.assertIsNotNone(server._server_thread)
            self.assertTrue(server._server_thread.is_alive())

        # After context exit, verify server and thread have shut down cleanly
        self.assertIsNone(fixture._server)
        self.assertIsNone(fixture._server_thread)
        self.assertIsNone(fixture._port)
        with self.assertRaises(RuntimeError):
            _ = fixture.port

    def test_synthetic_get_handling(self) -> None:
        """2. Verify GET /job returns HTTP 200 with synthetic fixture and unknown path returns 404."""
        with LocalMockServerFixture() as server:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
            try:
                # Request valid job listing
                conn.request("GET", "/job")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                body = response.read().decode("utf-8")
                # Assert stable markers from synthetic fixture
                self.assertIn("Synthetic Technologies Ltd.", body)
                self.assertIn("Frontend Developer", body)
                self.assertIn("jobApplicationForm", body)

                # Request unknown path
                conn.request("GET", "/nonexistent-test-path")
                response_404 = conn.getresponse()
                self.assertEqual(response_404.status, 404)
                body_404 = response_404.read().decode("utf-8")
                self.assertIn("404 Not Found", body_404)
            finally:
                conn.close()

    def test_proxy_style_request_rejection(self) -> None:
        """3. Verify absolute-form proxy request targets sent to 127.0.0.1 are rejected with HTTP 400."""
        with LocalMockServerFixture() as server:
            # Case A: http:// absolute proxy target
            conn_http = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
            try:
                conn_http.request("GET", "http://example.invalid/path")
                response_http = conn_http.getresponse()
                self.assertEqual(response_http.status, 400)
                _ = response_http.read()
            finally:
                conn_http.close()

            # Case B: https:// absolute proxy target
            conn_https = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
            try:
                conn_https.request("GET", "https://example.invalid/path")
                response_https = conn_https.getresponse()
                self.assertEqual(response_https.status, 400)
                _ = response_https.read()
            finally:
                conn_https.close()

            # Case C: // protocol-relative target sent as raw HTTP request line via socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            try:
                sock.connect(("127.0.0.1", server.port))
                raw_request = (
                    b"GET //example.invalid/path HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Connection: close\r\n\r\n"
                )
                sock.sendall(raw_request)

                # Read response robustly with bounded reads until EOF or headers end
                response_data = b""
                while True:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        response_data += chunk
                        if b"\r\n\r\n" in response_data:
                            break
                    except (socket.timeout, OSError):
                        break

                # Robustly parse the HTTP status line
                self.assertTrue(len(response_data) > 0, "No response data received from mock server")
                status_line = response_data.split(b"\r\n")[0].decode("latin-1")
                parts = status_line.split(" ")
                self.assertGreaterEqual(len(parts), 2, f"Malformed HTTP status line: {status_line}")
                status_code = int(parts[1])
                self.assertEqual(status_code, 400)
            finally:
                sock.close()

    def test_synthetic_post_handling(self) -> None:
        """4. Verify synthetic POST /apply returns HTTP 200 confirmation without persistence."""
        with LocalMockServerFixture() as server:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
            try:
                # Send synthetic form submission payload with fake test data
                synthetic_payload = b"fullName=Synthetic+Test+User&email=test%40example.invalid"
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(synthetic_payload)),
                }
                conn.request("POST", "/apply", body=synthetic_payload, headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                body = response.read().decode("utf-8")
                self.assertIn("Application Submitted!", body)
            finally:
                conn.close()

    def test_unknown_post_path(self) -> None:
        """5. Verify synthetic POST to an unmapped path returns HTTP 404."""
        with LocalMockServerFixture() as server:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
            try:
                synthetic_payload = b"data=placeholder"
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(synthetic_payload)),
                }
                conn.request("POST", "/unknown-endpoint-xyz", body=synthetic_payload, headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                body = response.read().decode("utf-8")
                self.assertIn("404 Not Found", body)
            finally:
                conn.close()

    def test_head_handling(self) -> None:
        """6. Verify HEAD requests return headers without body for known and unknown paths."""
        with LocalMockServerFixture() as server:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
            try:
                conn.request("HEAD", "/job")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                body = response.read()
                self.assertEqual(body, b"")

                conn.request("HEAD", "/nonexistent-test-path")
                response_404 = conn.getresponse()
                self.assertEqual(response_404.status, 404)
                body_404 = response_404.read()
                self.assertEqual(body_404, b"")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
