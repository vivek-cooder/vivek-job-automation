# Offline Test Harness for Browser Safety Testing

## 1. Purpose and Scope

This package (`tests/offline_harness`) provides a test-only, isolated local test harness architecture designed for future browser safety testing.

The primary objective is to allow browser navigation and form validation routines to eventually be tested against realistic, controlled web pages without ever contacting external networks, third-party job portals, or live infrastructure.

> **CRITICAL STATUS NOTICE**  
> In Phase 22, this harness was created under **STRICT MODE: CODE CHANGES ONLY — DO NOT EXECUTE**.  
> No tests were run, no servers were started, no sockets were bound, no browsers were launched, and no network traffic was generated.  
> Runtime execution remains **unverified** and requires separate, explicit authorization in a future phase.

---

## 2. Core Safety Boundaries and Architecture

### A. Synthetic-Only Data Rule
- All page fixtures in `mock_pages.py` are strictly synthetic.
- Fictional employer names (`Apex Orion Technologies Inc.`, etc.), simulated contacts, and placeholder URLs (`example.invalid`) are used exclusively.
- No real candidate details, credentials, API keys, session tokens, or live application history records may ever be included in or processed by the harness.

### B. Local-Only Server Boundary
- `LocalMockServerFixture` in `server_fixture.py` binds exclusively to `127.0.0.1`.
- It uses operating-system-assigned ephemeral ports (port `0`) to eliminate port collisions and ensure test isolation.
- Binding to `0.0.0.0`, `::`, LAN addresses, or external interfaces is strictly prohibited and guarded at runtime by assertion checks.
- The server serves only the in-memory static fixtures defined in `mock_pages.py`. It rejects forwarding/proxy requests.
- Form submissions (`POST`) are not stored on disk or logged; request bodies and query parameters are discarded to prevent data leakage.

### C. Default-Deny URL and Network Policy
- `HarnessRequestPolicy` in `request_policy.py` enforces strict default-deny evaluation on every network request attempt.
- The policy allows **only** requests matching scheme `http://` targeting host `127.0.0.1` and the specific assigned ephemeral port passed by the test fixture.
- All non-HTTP schemes (`https`, `file`, `javascript`, `data`, `blob`), external hostnames, domain names (even `localhost`), IP wildcards, and unexpected ports are rejected.
- The policy operates in-memory with zero DNS lookups and zero external calls.

### D. Isolation from Production Data and Browser Profiles
- **No Production Browser Profiles:** Any future browser test using this harness must spawn a temporary, non-persistent browser context (`launch()` or `browser.new_context()` without `--user-data-dir`). Production user data directories (such as `profile/`) must never be loaded.
- **No Production Queue or History:** Tests must never import or read from `data/application_queue.json`, `data/applications.json`, or `config.json`.
- **No Production Navigation Settings:** The production navigation configuration (`navigation.allowed_hosts = []` and `auto_submit = false`) remains untouched and must not be bypassed or altered.

---

## 3. Defense-in-Depth and Network Limitations

Browser routing rules (e.g. Playwright route interception) are a powerful safeguard, but **they do not provide absolute proof of zero network activity on their own**. 

Potential edge cases:
1. Browser internal processes (telemetry, certificate revocation checks, DNS pre-fetching) can theoretically bypass page-level route handlers if not suppressed with browser launch flags (`--disable-background-networking`, `--disable-component-update`, etc.).
2. Service workers or WebSockets may require explicit interception.
3. Therefore, both browser route handlers AND the mock server's strict loopback isolation must operate together as layered defenses.

---

## 4. Proposed Future Safety Verification Workflow

When authorized for execution in a future phase, the following step-by-step checks should be conducted:

1. **Unit Test Verification (Pure Python, No Browser):**
   - Verify `HarnessRequestPolicy` correctly rejects external domains, improper schemes, non-127.0.0.1 hosts, and incorrect ports.
   - Verify `mock_pages.py` contains valid HTML syntax and no active outbound external scripts.
2. **In-Process Server Verification (Local Loopback Only):**
   - Verify `LocalMockServerFixture` starts and stops cleanly on an ephemeral port.
   - Verify unknown endpoints return `404 Not Found`.
3. **Isolated Headless Browser Verification:**
   - Launch an isolated browser context with network route interception directing strictly to the fixture's base URL.
   - Verify page navigation to `/job` renders the synthetic job page.
   - Verify that simulated outbound calls (e.g., to `http://example.invalid/analytics.js`) are caught and blocked by the harness policy.
   - Verify that the browser context terminates without saving state or creating disk artifacts in the repository.

*Note: None of the above checks have been executed during Phase 22. They represent the protocol for future authorized phases.*
