"""Offline Test Harness for Vivek Job Automation.

This package contains test-only, inert fixtures and request isolation policies
for future local browser safety testing.
All fixtures are strictly synthetic. No sockets are bound, no servers are started,
and no network connections are made at import time.
"""

from tests.offline_harness.mock_pages import MOCK_PAGES
from tests.offline_harness.request_policy import (
    HarnessRequestPolicy,
    PolicyDecision,
    is_safe_harness_request,
)
from tests.offline_harness.server_fixture import LocalMockServerFixture

__all__ = [
    "MOCK_PAGES",
    "HarnessRequestPolicy",
    "PolicyDecision",
    "is_safe_harness_request",
    "LocalMockServerFixture",
]
