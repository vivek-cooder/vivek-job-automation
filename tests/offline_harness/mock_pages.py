"""Synthetic HTML mock fixtures for offline browser safety testing.

All content in this module is 100% fictional and synthetic.
No real employer information, personal profiles, credentials, or active external URLs
are contained in these fixtures. External reference scenarios use reserved RFC 2606
domains (e.g., example.invalid, external.invalid) that cannot resolve.
"""

from typing import Dict

# 1. Standard valid job listing with embedded application form
PAGE_VALID_JOB_LISTING = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Frontend Developer - Synthetic Technologies Ltd.</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; }
    .form-group { margin-bottom: 1rem; }
    label { display: block; font-weight: bold; }
    input, select { width: 100%; max-width: 400px; padding: 0.5rem; }
    button { padding: 0.5rem 1rem; background: #0066cc; color: #fff; border: none; cursor: pointer; }
  </style>
</head>
<body>
  <h1>Frontend Developer</h1>
  <p class="company">Synthetic Technologies Ltd. — Mumbai, India (Remote)</p>
  <div class="description">
    <p>We are looking for an entry-level React developer with 0-1 years of experience.</p>
  </div>

  <h2>Apply for this Position</h2>
  <form id="jobApplicationForm" action="/mock-apply-endpoint" method="POST">
    <div class="form-group">
      <label for="fullName">Full Name *</label>
      <input type="text" id="fullName" name="fullName" required>
    </div>
    <div class="form-group">
      <label for="email">Email Address *</label>
      <input type="email" id="email" name="email" required>
    </div>
    <div class="form-group">
      <label for="phone">Phone Number *</label>
      <input type="tel" id="phone" name="phone" required>
    </div>
    <div class="form-group">
      <label for="position">Position *</label>
      <select id="position" name="position" required>
        <option value="">Select a position...</option>
        <option value="Frontend Developer" selected>Frontend Developer</option>
        <option value="Backend Developer">Backend Developer</option>
      </select>
    </div>
    <div class="form-group">
      <label for="portfolioUrl">Portfolio URL</label>
      <input type="url" id="portfolioUrl" name="portfolioUrl">
    </div>
    <div class="form-group">
      <label for="linkedinUrl">LinkedIn Profile URL</label>
      <input type="url" id="linkedinUrl" name="linkedinUrl">
    </div>
    <button type="submit" id="submitApplicationButton">Submit Application</button>
  </form>
</body>
</html>
"""

# 2. Listing with missing or malformed title
PAGE_MALFORMED_TITLE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title></title>
</head>
<body>
  <h1 class="header-empty"></h1>
  <p class="company">Acme Mock Corp</p>
  <p>Listing content without any identifiable job title or header.</p>
  <form id="applyForm" action="/mock-apply-endpoint" method="POST">
    <input type="text" name="name" placeholder="Name">
    <button type="submit">Submit</button>
  </form>
</body>
</html>
"""

# 3. Listing with an ambiguous application action (generic button text)
PAGE_AMBIGUOUS_ACTION = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Ambiguous Role - Synthetic Corp</title>
</head>
<body>
  <h1>Frontend Engineer</h1>
  <p>Synthetic Corp</p>
  <div class="actions">
    <button type="button" class="btn" id="genericButton1">Click Here</button>
    <button type="button" class="btn" id="genericButton2">Continue</button>
    <a href="/generic-link" id="ambiguousLink">Learn More</a>
  </div>
</body>
</html>
"""

# 4. Listing with unexpected required form fields
PAGE_UNEXPECTED_REQUIRED_FIELDS = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Software Engineer - Synthetic Corp</title>
</head>
<body>
  <h1>Software Engineer</h1>
  <form id="customForm" action="/mock-apply-endpoint" method="POST">
    <label for="name">Name *</label>
    <input type="text" id="name" name="name" required>
    
    <label for="customTaxId">National Tax Registration Code *</label>
    <input type="text" id="customTaxId" name="customTaxId" required>
    
    <label for="securityClearance">Military Clearance Passcode *</label>
    <input type="text" id="securityClearance" name="securityClearance" required>
    
    <button type="submit">Submit Application</button>
  </form>
</body>
</html>
"""

# 5. Form targeting an external action URL (hazard detection fixture)
PAGE_EXTERNAL_FORM_ACTION = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>External Action Hazard - Synthetic Corp</title>
</head>
<body>
  <h1>Web Developer</h1>
  <!-- Action points to reserved external domain to test interception -->
  <form id="externalActionForm" action="https://external.invalid/submit-application" method="POST">
    <input type="text" name="name" value="Test Candidate">
    <button type="submit" id="externalSubmit">Submit Application</button>
  </form>
</body>
</html>
"""

# 6. Page containing external asset references (images / scripts)
PAGE_EXTERNAL_ASSETS = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>External Asset Hazard - Synthetic Corp</title>
  <!-- Remote stylesheet that must be intercepted and blocked -->
  <link rel="stylesheet" href="https://external.invalid/styles/theme.css">
  <!-- Remote analytics script that must be intercepted and blocked -->
  <script src="https://external.invalid/analytics.js"></script>
</head>
<body>
  <h1>Frontend Designer</h1>
  <!-- Remote tracking beacon that must be intercepted and blocked -->
  <img src="https://external.invalid/beacons/tracker.png" alt="Beacon">
  <p>Page containing external resource tags for request interception testing.</p>
</body>
</html>
"""

# 7. Page with JavaScript attempting an external fetch request
PAGE_EXTERNAL_FETCH = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>External Fetch Hazard - Synthetic Corp</title>
</head>
<body>
  <h1>Junior Developer</h1>
  <p>Testing dynamic JavaScript network calls.</p>
  <script>
    // Dynamic fetch that must be blocked by the harness policy
    try {
      fetch('https://external.invalid/api/telemetry', {
        method: 'POST',
        body: JSON.stringify({ event: 'page_view' })
      });
    } catch (e) {
      // Expected to fail in an isolated environment
    }
  </script>
</body>
</html>
"""

# 8. Page with a simulated security challenge / verification gate
PAGE_SECURITY_CHALLENGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Security Verification Required</title>
</head>
<body>
  <div id="challenge-form">
    <h1>Security Challenge</h1>
    <p>Please complete the human verification check before accessing the job application.</p>
    <div class="g-recaptcha" data-sitekey="synthetic-test-key-0000"></div>
  </div>
</body>
</html>
"""

# 9. Page with sensitive query string patterns in links
PAGE_QUERY_SECRETS = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sensitive Query Test - Synthetic Corp</title>
</head>
<body>
  <h1>Careers Portal</h1>
  <!-- Link with synthetic tokens to verify URL query parameter stripping -->
  <a href="/job?access_token=SYNTHETIC_TEST_TOKEN&candidate_email=candidate@example.invalid#step2" id="secretLink">
    Proceed to Application
  </a>
</body>
</html>
"""

# 10. Synthetic application success confirmation page
PAGE_APPLICATION_SUCCESS = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Application Submitted - Synthetic Technologies Ltd.</title>
</head>
<body>
  <div id="submitSuccess">
    <h1>Application Submitted!</h1>
    <p>Thank you for your application. We have received your submission.</p>
  </div>
</body>
</html>
"""

# 11. Minimal robots.txt fixture
PAGE_ROBOTS_TXT = """User-agent: *
Disallow: /private/
"""

# Dictionary mapping local mock server paths to HTML fixture content
MOCK_PAGES: Dict[str, str] = {
    "/": PAGE_VALID_JOB_LISTING,
    "/job": PAGE_VALID_JOB_LISTING,
    "/careers/frontend-developer": PAGE_VALID_JOB_LISTING,
    "/malformed-title": PAGE_MALFORMED_TITLE,
    "/ambiguous-action": PAGE_AMBIGUOUS_ACTION,
    "/unexpected-fields": PAGE_UNEXPECTED_REQUIRED_FIELDS,
    "/external-form-action": PAGE_EXTERNAL_FORM_ACTION,
    "/external-assets": PAGE_EXTERNAL_ASSETS,
    "/external-fetch": PAGE_EXTERNAL_FETCH,
    "/security-challenge": PAGE_SECURITY_CHALLENGE,
    "/query-secrets": PAGE_QUERY_SECRETS,
    "/apply-success": PAGE_APPLICATION_SUCCESS,
    "/mock-apply-endpoint": PAGE_APPLICATION_SUCCESS,
    "/robots.txt": PAGE_ROBOTS_TXT,
}
