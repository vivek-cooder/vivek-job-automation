"""Optional, manual live-provider smoke test using only the local HTML fixture."""

from pathlib import Path

from worker.ai.browser_agent import BrowserAgent
from worker.ai.provider import create_provider
from worker.ai.settings import AISettings


def main():
    settings = AISettings.from_environment()
    if not settings.enabled or not settings.api_key or not settings.model or not settings.provider:
        print("AI live test skipped — credentials/configuration not configured.")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("AI live test blocked — Playwright is unavailable in this Python environment.")
        return 2

    fixture = Path(__file__).resolve().parents[1] / "test_application.html"
    print(f"AI LOCAL FIXTURE TEST ONLY: {fixture.name}; no submit or browser actions will run.")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(fixture.as_uri(), wait_until="load")
            agent = BrowserAgent(create_provider(settings), settings)
            snapshot = agent._snapshot(page, __import__("worker.form_adapter", fromlist=["analyze_form"]).analyze_form(page))
            plan = agent._validate_plan(agent.provider.analyze_page(snapshot), snapshot)
            print(f"AI local fixture analysis completed; page_type={plan['page_type']}; "
                  f"confidence={plan['confidence']:.2f}; actions=0; submission=disabled.")
            browser.close()
        return 0
    except Exception as exc:
        print(f"AI local fixture test failed safely: {type(exc).__name__}.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
