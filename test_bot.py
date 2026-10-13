from pathlib import Path
from playwright.sync_api import sync_playwright
from worker.applicant import load_applicant_profile

BASE_DIR = Path(__file__).resolve().parent
TEST_PAGE = BASE_DIR / "test_application.html"

APPLICANT = load_applicant_profile()


with sync_playwright() as p:

    print("Starting automation...")

    browser = p.chromium.launch(headless=False)

    page = browser.new_page(
        viewport={
            "width": 1400,
            "height": 900
        }
    )

    # Open our test application
    page.goto(TEST_PAGE.as_uri())

    print("Test application opened.")

    # Fill application
    page.locator("#name").fill(APPLICANT["name"])
    page.locator("#email").fill(APPLICANT["email"])
    page.locator("#phone").fill(APPLICANT["phone"])
    page.locator("#location").fill(APPLICANT["location"])
    page.locator("#experience").fill(APPLICANT["experience"])

    print("Application fields filled automatically.")

    print("")
    print("========================================")
    print(" HUMAN VERIFICATION REQUIRED")
    print("========================================")
    print("Complete the verification in Chromium.")
    print("Use test code: 123456")
    print("")

    # Wait until verification input becomes disabled.
    page.wait_for_function("""
        () => document.querySelector("#verificationCode").disabled === true
    """)

    print("Verification completed!")

    # Submit the test application
    page.locator("button[type='submit']").click()

    # Wait for success message
    page.locator("#success").wait_for(state="visible")

    print("")
    print("========================================")
    print(" APPLICATION COMPLETED SUCCESSFULLY")
    print("========================================")

    page.wait_for_timeout(5000)

    browser.close()
