from pathlib import Path
from playwright.sync_api import sync_playwright
from worker.navigation import configured_policy


BASE_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = BASE_DIR / "profile"


def start_browser(playwright, navigation_policy=None):

    print("Starting browser...")
    print(f"Profile: {PROFILE_DIR.name}")

    navigation_policy = navigation_policy or configured_policy()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        service_workers="block",
        viewport={
            "width": 1400,
            "height": 900
        }
    )

    # Context-level routing sees page, popup, subresource, and redirect requests.
    navigation_policy.install(context)

    print("Browser context started.")

    # Use the first page created by Chromium.
    if context.pages:
        page = context.pages[0]
    else:
        print("No initial page exists. Creating one...")
        page = context.new_page()

    print(f"Page ready: {page.url}")

    return None, context, page


if __name__ == "__main__":

    with sync_playwright() as playwright:

        browser, context, page = start_browser(playwright)

        page.goto("https://www.google.com")

        print()
        print("====================================")
        print(" Browser is running")
        print(" Close the browser window to stop")
        print("====================================")

        try:
            page.wait_for_timeout(3600000)
        except Exception:
            pass
