"""Default-deny request authorization policy for local mock test harness.

Strictly confines all permitted network requests to http://127.0.0.1:<expected_port>.
Rejects all external hosts, public IPs, non-HTTP schemes, mismatched ports,
and malformed URLs without performing DNS resolution or external network calls.
"""

from dataclasses import dataclass
from typing import Any, Tuple
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PolicyDecision:
    """Structured decision returned by the harness request policy."""
    allowed: bool
    reason: str
    target_host: str = ""
    target_port: int = 0


class HarnessRequestPolicy:
    """Default-deny policy enforced on future mock-server browser contexts.

    Guarantees:
    - Never permits connections outside 127.0.0.1.
    - Requires an explicit, valid TCP port matching the ephemeral mock server.
    - Rejects wildcards, 0.0.0.0, LAN addresses, public domains, and localhost aliases.
    - Never reads or alters production config.json or navigation.allowed_hosts.
    - Does not perform network requests or DNS resolution.
    - Safe reason codes never expose URL query strings, credentials, or PII.
    """

    ALLOWED_IP = "127.0.0.1"
    ALLOWED_SCHEME = "http"

    def __init__(self, expected_port: int):
        if not isinstance(expected_port, int) or isinstance(expected_port, bool):
            raise ValueError("expected_port must be an integer")
        if not (1 <= expected_port <= 65535):
            raise ValueError(f"expected_port must be a valid TCP port (1-65535), got {expected_port}")
        self.expected_port = expected_port

    def evaluate(self, url: Any) -> PolicyDecision:
        """Evaluate an arbitrary target URL against the local-only policy."""
        if not url:
            return PolicyDecision(allowed=False, reason="EMPTY_URL")

        try:
            raw = str(url).strip()
            if not raw:
                return PolicyDecision(allowed=False, reason="EMPTY_URL")

            parts = urlsplit(raw)
        except Exception:
            return PolicyDecision(allowed=False, reason="MALFORMED_URL")

        # 1. Scheme check: only unencrypted http is permitted to the local loopback mock
        scheme = (parts.scheme or "").lower()
        if scheme != self.ALLOWED_SCHEME:
            if scheme in {"https", "ftp", "file", "javascript", "data", "ws", "wss"}:
                return PolicyDecision(allowed=False, reason=f"DISALLOWED_SCHEME_{scheme.upper()}")
            return PolicyDecision(allowed=False, reason="UNSUPPORTED_SCHEME")

        # 2. Credential check: URLs with embedded user/password are rejected immediately
        if parts.username or parts.password:
            return PolicyDecision(allowed=False, reason="CREDENTIALS_IN_URL_REJECTED")

        # 3. Hostname check: strictly 127.0.0.1 (rejects localhost, 0.0.0.0, IPv6 ::1, LAN, remote domains)
        hostname = (parts.hostname or "").lower()
        if hostname != self.ALLOWED_IP:
            if not hostname:
                return PolicyDecision(allowed=False, reason="MISSING_HOSTNAME")
            if hostname == "localhost":
                # Disallow 'localhost' to prevent reliance on OS /etc/hosts or IPv6 resolution ambiguity
                return PolicyDecision(allowed=False, reason="LOCALHOST_NAME_NOT_PERMITTED_USE_127_0_0_1")
            return PolicyDecision(allowed=False, reason="EXTERNAL_OR_NON_LOOPBACK_HOST_DENIED", target_host=hostname)

        # 4. Port check: must explicitly match the ephemeral mock server port
        try:
            port = parts.port
        except ValueError:
            return PolicyDecision(allowed=False, reason="MALFORMED_PORT")

        if port is None:
            # Default http port 80 is rejected unless the server was explicitly bound to 80 (not ephemeral)
            if self.expected_port != 80:
                return PolicyDecision(allowed=False, reason="DEFAULT_PORT_80_DENIED", target_host=hostname, target_port=80)
            port = 80

        if port != self.expected_port:
            return PolicyDecision(
                allowed=False,
                reason="PORT_MISMATCH_DENIED",
                target_host=hostname,
                target_port=port,
            )

        return PolicyDecision(
            allowed=True,
            reason="AUTHORIZED_LOCAL_LOOPBACK",
            target_host=hostname,
            target_port=port,
        )


def is_safe_harness_request(url: Any, expected_port: int) -> Tuple[bool, str]:
    """Convenience functional helper returning (is_allowed, safe_reason)."""
    try:
        policy = HarnessRequestPolicy(expected_port=expected_port)
        decision = policy.evaluate(url)
        return decision.allowed, decision.reason
    except ValueError as exc:
        return False, f"POLICY_CONFIG_ERROR: {str(exc)}"
    except Exception:
        return False, "EVALUATION_ERROR"
