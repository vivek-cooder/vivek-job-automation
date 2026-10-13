"""Local-only tests for exact navigation host authorization and request blocking."""

import unittest
from unittest.mock import Mock
from unittest.mock import patch

from worker.navigation import NavigationPolicy, NavigationRejected, normalize_hostname


class NavigationPolicyTests(unittest.TestCase):
    def test_explicit_hostname_and_explicit_www_alias(self):
        policy = NavigationPolicy(["NovamiInfotechs.com", "www.novamiinfotechs.com"])
        self.assertTrue(policy.check("https://novamiinfotechs.com/jobs/1"))
        self.assertTrue(policy.check("https://www.novamiinfotechs.com/jobs/1"))

    def test_www_is_not_implicitly_authorized(self):
        policy = NavigationPolicy(["novamiinfotechs.com"])
        with self.assertRaisesRegex(NavigationRejected, "UNAUTHORIZED_HOST"):
            policy.check("https://www.novamiinfotechs.com/jobs/1")

    def test_deceptive_and_similar_domains_are_rejected(self):
        policy = NavigationPolicy(["novamiinfotechs.com", "www.novamiinfotechs.com"])
        for url in (
            "https://novamiinfotechs.com.attacker.invalid/apply",
            "https://novamiinfotechs.net/apply",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(NavigationRejected, "UNAUTHORIZED_HOST"):
                policy.check(url)

    def test_hostname_normalization_is_exact_and_not_suffix_based(self):
        self.assertEqual(normalize_hostname("WWW.NOVAMIINFOTECHS.COM."), "www.novamiinfotechs.com")
        self.assertEqual(
            normalize_hostname("www.novamiinfotechs.com.attacker.invalid"),
            "www.novamiinfotechs.com.attacker.invalid",
        )

    def test_unsupported_schemes_and_url_userinfo_fail_closed(self):
        policy = NavigationPolicy(["novamiinfotechs.com"])
        for url in (
            "javascript:alert(1)",
            "https://novamiinfotechs.com@attacker.invalid/apply",
        ):
            with self.subTest(url=url), self.assertRaises(NavigationRejected):
                policy.check(url)

    def test_context_route_blocks_unauthorized_redirect_hop(self):
        """Exercise the installed route callback without opening a socket."""
        policy = NavigationPolicy(["novamiinfotechs.com"])
        context = Mock()
        guard = policy.install(context)
        self.assertEqual(context.route.call_args.args[0], "**/*")

        allowed_hop = Mock()
        allowed_hop.request.url = "https://novamiinfotechs.com/job"
        guard(allowed_hop)
        allowed_hop.continue_.assert_called_once_with()
        allowed_hop.abort.assert_not_called()

        redirected_hop = Mock()
        redirected_hop.request.url = "https://www.attacker.invalid/landing"
        guard(redirected_hop)
        redirected_hop.abort.assert_called_once_with("blockedbyclient")
        redirected_hop.continue_.assert_not_called()
        self.assertEqual(policy.last_rejection, "UNAUTHORIZED_HOST")

    def test_local_test_policy_is_explicit_and_production_default_is_empty(self):
        self.assertFalse(NavigationPolicy().allowed_hosts)
        local = NavigationPolicy.for_local_tests()
        self.assertTrue(local.check("http://127.0.0.1:8000/fixture"))
        self.assertTrue(local.check("file:///C:/project/tests/fixtures/page.html"))
        with self.assertRaises(NavigationRejected):
            NavigationPolicy().check("file:///C:/project/tests/fixtures/page.html")

    def test_worker_rejects_unapproved_queued_url_before_goto(self):
        from worker import runner

        page = Mock()
        page.url = "about:blank"
        job = {
            "id": "navigation-policy-test", "source": "local-test",
            "company": "Local Test", "title": "Frontend Developer",
            "url": "https://allowed.example.attacker.invalid/jobs/1",
        }
        policy = NavigationPolicy(["allowed.example"])
        with patch.object(runner, "validate_job", return_value=(True, None)), \
             patch.object(runner, "find_protected_queue_duplicate", return_value=None), \
             patch.object(runner, "load_queue", return_value=[]), \
             patch.object(runner, "is_duplicate_application", return_value=(False, None)), \
             patch.object(runner, "mark_job_status"), \
             patch.object(runner, "save_application"), \
             patch.object(runner, "update_application_status"):
            result = runner.process_job(page, job, navigation_policy=policy)
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_reason"], "UNAUTHORIZED_HOST")
        page.goto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
