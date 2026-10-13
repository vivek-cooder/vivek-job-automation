"""Pure offline unit tests for HarnessRequestPolicy.

Validates default-deny URL parsing and request policy logic using synthetic strings only.
Zero network access, zero socket binding, zero server startup, zero browser interaction.
"""

import unittest

from tests.offline_harness.request_policy import (
    HarnessRequestPolicy,
    PolicyDecision,
    is_safe_harness_request,
)


class HarnessRequestPolicyTests(unittest.TestCase):
    """Unit tests for the default-deny request policy against synthetic URLs."""

    # Fixed example ephemeral port for all tests
    TEST_PORT = 43127

    def setUp(self) -> None:
        self.policy = HarnessRequestPolicy(expected_port=self.TEST_PORT)

    def test_well_formed_http_url_allowed(self) -> None:
        """1. A well-formed HTTP URL for 127.0.0.1 and the expected port is allowed."""
        urls = [
            f"http://127.0.0.1:{self.TEST_PORT}/",
            f"http://127.0.0.1:{self.TEST_PORT}/job",
            f"http://127.0.0.1:{self.TEST_PORT}/careers/frontend-developer",
            f"http://127.0.0.1:{self.TEST_PORT}/apply-success",
        ]
        for url in urls:
            with self.subTest(url=url):
                decision = self.policy.evaluate(url)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "AUTHORIZED_LOCAL_LOOPBACK")
                self.assertEqual(decision.target_host, "127.0.0.1")
                self.assertEqual(decision.target_port, self.TEST_PORT)

                # Verify helper function matches
                allowed, reason = is_safe_harness_request(url, expected_port=self.TEST_PORT)
                self.assertTrue(allowed)
                self.assertEqual(reason, "AUTHORIZED_LOCAL_LOOPBACK")

    def test_different_port_denied(self) -> None:
        """2. A different port is denied."""
        mismatched_urls = [
            "http://127.0.0.1:8080/job",
            "http://127.0.0.1:43128/job",
            "http://127.0.0.1:3000/job",
            "http://127.0.0.1/job",  # Implicit port 80
        ]
        for url in mismatched_urls:
            with self.subTest(url=url):
                decision = self.policy.evaluate(url)
                self.assertFalse(decision.allowed)
                self.assertIn(decision.reason, {"PORT_MISMATCH_DENIED", "DEFAULT_PORT_80_DENIED"})

    def test_disallowed_schemes_denied(self) -> None:
        """3. https, file, javascript, data, and other non-http schemes are denied."""
        scheme_urls = [
            (f"https://127.0.0.1:{self.TEST_PORT}/job", "DISALLOWED_SCHEME_HTTPS"),
            ("file:///etc/passwd", "DISALLOWED_SCHEME_FILE"),
            ("file://C:/Windows/System32", "DISALLOWED_SCHEME_FILE"),
            ("javascript:alert(1)", "DISALLOWED_SCHEME_JAVASCRIPT"),
            ("data:text/html,<h1>test</h1>", "DISALLOWED_SCHEME_DATA"),
            (f"ftp://127.0.0.1:{self.TEST_PORT}/files", "DISALLOWED_SCHEME_FTP"),
            (f"ws://127.0.0.1:{self.TEST_PORT}/socket", "DISALLOWED_SCHEME_WS"),
            (f"wss://127.0.0.1:{self.TEST_PORT}/socket", "DISALLOWED_SCHEME_WSS"),
            (f"gopher://127.0.0.1:{self.TEST_PORT}/", "UNSUPPORTED_SCHEME"),
        ]
        for url, expected_reason in scheme_urls:
            with self.subTest(url=url):
                decision = self.policy.evaluate(url)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, expected_reason)

    def test_non_loopback_and_alternative_hosts_denied(self) -> None:
        """4. localhost, 127.0.0.2, 0.0.0.0, [::1], and external hostnames are denied."""
        host_urls = [
            (f"http://localhost:{self.TEST_PORT}/job", "LOCALHOST_NAME_NOT_PERMITTED_USE_127_0_0_1"),
            (f"http://127.0.0.2:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
            (f"http://0.0.0.0:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
            (f"http://[::1]:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
            (f"http://192.168.1.1:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
            (f"http://10.0.0.1:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
            (f"http://example.com:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
            (f"http://example.invalid:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
            (f"http://novamiinfotechs.com:{self.TEST_PORT}/job", "EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED"),
        ]
        for url, expected_reason in host_urls:
            with self.subTest(url=url):
                decision = self.policy.evaluate(url)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, expected_reason)

    def test_credentials_in_url_denied(self) -> None:
        """5. URL user-info credentials are denied."""
        credential_urls = [
            f"http://user:pass@127.0.0.1:{self.TEST_PORT}/job",
            f"http://admin@127.0.0.1:{self.TEST_PORT}/job",
            f"http://candidate:secret123@127.0.0.1:{self.TEST_PORT}/",
        ]
        for url in credential_urls:
            with self.subTest(url=url):
                decision = self.policy.evaluate(url)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "CREDENTIALS_IN_URL_REJECTED")

    def test_invalid_expected_ports_denied_safely(self) -> None:
        """6. Missing or invalid expected ports are rejected safely during policy configuration."""
        invalid_ports = [0, -1, 65536, 100000, "43127", True, False, None]
        for bad_port in invalid_ports:
            with self.subTest(bad_port=bad_port):
                with self.assertRaises(ValueError):
                    HarnessRequestPolicy(expected_port=bad_port)  # type: ignore

                # Functional helper handles config errors safely without unhandled exception
                allowed, reason = is_safe_harness_request(
                    f"http://127.0.0.1:43127/job",
                    expected_port=bad_port,  # type: ignore
                )
                self.assertFalse(allowed)
                self.assertTrue(reason.startswith("POLICY_CONFIG_ERROR"))

    def test_malformed_and_out_of_range_url_ports(self) -> None:
        """7. Malformed and out-of-range URL ports are denied without uncaught exceptions."""
        malformed_port_urls = [
            "http://127.0.0.1:999999/job",
            "http://127.0.0.1:99999999999/job",
            "http://127.0.0.1:not_a_port/job",
        ]
        for url in malformed_port_urls:
            with self.subTest(url=url):
                decision = self.policy.evaluate(url)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "MALFORMED_PORT")

    def test_relative_urls_and_empty_input(self) -> None:
        """8. Relative URLs and empty input are denied safely."""
        empty_or_relative = [
            ("", "EMPTY_URL"),
            ("   ", "EMPTY_URL"),
            (None, "EMPTY_URL"),
            ("/job", "UNSUPPORTED_SCHEME"),
            ("careers/frontend-developer", "UNSUPPORTED_SCHEME"),
            (f"//127.0.0.1:{self.TEST_PORT}/job", "UNSUPPORTED_SCHEME"),
        ]
        for input_val, expected_reason in empty_or_relative:
            with self.subTest(input_val=input_val):
                decision = self.policy.evaluate(input_val)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, expected_reason)

    def test_query_strings_and_fragments_never_reflected_in_reasons(self) -> None:
        """9. Sensitive query parameters, tokens, and fragments never appear in denial reasons."""
        sensitive_tokens = [
            "SECRET_BEARER_TOKEN_9999",
            "candidate_email_address_secret",
            "password_hash_placeholder",
            "my_private_fragment",
        ]
        test_url = (
            f"http://127.0.0.1:8080/job?"
            f"token={sensitive_tokens[0]}&"
            f"email={sensitive_tokens[1]}&"
            f"hash={sensitive_tokens[2]}#"
            f"{sensitive_tokens[3]}"
        )
        decision = self.policy.evaluate(test_url)
        self.assertFalse(decision.allowed)

        # Verify none of the sensitive tokens leaked into the decision reason
        for token in sensitive_tokens:
            self.assertNotIn(token, decision.reason)

    def test_denial_reasons_are_safe_bounded_strings(self) -> None:
        """10. Denial reasons are strictly safe, bounded, uppercase identifier strings."""
        sample_urls = [
            "https://external.invalid/job",
            "http://localhost:43127/",
            "http://127.0.0.1:8080/",
            "http://user:pass@127.0.0.1:43127/",
            "http://127.0.0.1:999999/",
            "",
            "/job",
        ]
        for url in sample_urls:
            with self.subTest(url=url):
                decision = self.policy.evaluate(url)
                self.assertIsInstance(decision.reason, str)
                self.assertLessEqual(len(decision.reason), 64)
                # Reason must be an uppercase identifier or category string, never containing URL syntax
                self.assertNotIn("://", decision.reason)
                self.assertNotIn("?", decision.reason)
                self.assertNotIn("&", decision.reason)


if __name__ == "__main__":
    unittest.main()
