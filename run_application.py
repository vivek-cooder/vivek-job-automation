from pathlib import Path
from playwright.sync_api import sync_playwright

from worker.applicant import load_applicant_profile
from worker.application import apply_to_job
from worker.navigation import NavigationPolicy


BASE_DIR = Path(__file__).resolve().parent
TEST_PAGE = BASE_DIR / "test_application.html"


# --------------------------------------------------
# Applicant information
# --------------------------------------------------

APPLICANT = load_applicant_profile()


# --------------------------------------------------
# Test job
# --------------------------------------------------

JOB = {
    "company": "Test Technologies",
    "job_title": "React Developer",
    "location": "Mumbai",
    "url": "https://example.com/job/123",
    "source": "Company Career Page",
}


# --------------------------------------------------
# Start browser
# --------------------------------------------------

with sync_playwright() as p:

    print("Starting browser...")

    browser = p.chromium.launch(
        headless=False
    )

    page = browser.new_page(
        viewport={
            "width": 1400,
            "height": 900
        }
    )

    # Open test application
    page.goto(TEST_PAGE.as_uri())

    print("Test application opened.")

    # --------------------------------------------------
    # Start application
    # --------------------------------------------------

    result = apply_to_job(
        page,
        JOB,
        APPLICANT,
        navigation_policy=NavigationPolicy.for_local_tests(),
    )
    print(f"Application preparation status: {result.get('status', 'Unknown')}")

    print()
    print("=" * 50)
    print("APPLICATION WORKFLOW FINISHED")
    print("=" * 50)
    print("Browser will remain open.")
    print("Press ENTER in the terminal to close it.")

    input()

    if not page.is_closed():
        browser.close()
