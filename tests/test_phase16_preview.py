"""Phase 16 — Local Human-Review Application Preview Tests.

Verifies:
1. Preview generation with synthetic data only.
2. All seven expected fields are present.
3. Required and optional status is correct.
4. Optional values remain blank.
5. No real profile values are read or emitted.
6. The job URL is displayed as plain text, not an actionable link.
7. No Apply, Submit, Autofill, Upload, or Execute controls are generated.
8. No scripts, external resources, or network-request logic appear in the preview.
9. HTML escaping works correctly.
10. Repeated generation produces deterministic output.
11. Protected production files remain unchanged.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.data_safety import preserve_project_data_test
from worker.phase16_preview import (
    PREVIEW_FIELDS,
    render_preview_html,
    generate_preview_file,
    DEFAULT_OUTPUT_PATH,
)

BASE_DIR = Path(__file__).resolve().parents[1]


class Phase16PreviewTests(unittest.TestCase):

    # 1. Preview generation with synthetic data only
    @preserve_project_data_test
    def test_preview_generation_synthetic_data(self):
        html_content = render_preview_html()
        self.assertIn("[SYNTHETIC CANDIDATE NAME]", html_content)
        self.assertIn("candidate@example.invalid", html_content)
        self.assertIn("[SYNTHETIC PHONE]", html_content)
        self.assertIn("Frontend Developer (Engineering)", html_content)

    # 2. All seven expected fields are present
    @preserve_project_data_test
    def test_all_seven_fields_present(self):
        html_content = render_preview_html()
        expected_fields = [
            "fullName",
            "email",
            "phone",
            "position",
            "portfolioUrl",
            "linkedinUrl",
            "coverLetter",
        ]
        for f in expected_fields:
            self.assertIn(f"<code>{f}</code>", html_content)
        self.assertEqual(len(PREVIEW_FIELDS), 7)

    # 3. Required and optional status is correct
    @preserve_project_data_test
    def test_required_and_optional_status(self):
        field_map = {f["website_field"]: f for f in PREVIEW_FIELDS}
        self.assertTrue(field_map["fullName"]["required"])
        self.assertTrue(field_map["email"]["required"])
        self.assertTrue(field_map["phone"]["required"])
        self.assertTrue(field_map["position"]["required"])

        self.assertFalse(field_map["portfolioUrl"]["required"])
        self.assertFalse(field_map["linkedinUrl"]["required"])
        self.assertFalse(field_map["coverLetter"]["required"])

    # 4. Optional values remain blank
    @preserve_project_data_test
    def test_optional_values_remain_blank(self):
        field_map = {f["website_field"]: f for f in PREVIEW_FIELDS}
        for opt_field in ("portfolioUrl", "linkedinUrl", "coverLetter"):
            status_desc = field_map[opt_field]["preview_value_status"]
            self.assertTrue(status_desc.startswith("Blank"))

    # 5. No real profile values are read or emitted
    @preserve_project_data_test
    def test_no_real_profile_values_emitted(self):
        # Read real profile to verify none of its non-empty strings are in preview
        profile_path = BASE_DIR / "profile" / "applicant.json"
        if profile_path.exists():
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            html_content = render_preview_html()
            for key, val in data.items():
                if val and isinstance(val, str) and len(val.strip()) > 2:
                    # Generic common words like 'Frontend' or 'Developer' might match job role,
                    # but personal strings (name, email, phone, location) must never appear.
                    if key in ("name", "email", "phone", "location"):
                        self.assertNotIn(
                            val.strip(), html_content,
                            f"Real profile value for '{key}' leaked into preview HTML!"
                        )

    # 6. Job URL is displayed as plain text, not an actionable link
    @preserve_project_data_test
    def test_job_url_displayed_as_plain_text(self):
        html_content = render_preview_html()
        url = "https://www.novamiinfotechs.com/careers/frontend-developer/"
        # Must be in text
        self.assertIn(url, html_content)
        # Must NOT be in <a href="...">
        self.assertNotIn(f'href="{url}"', html_content)
        self.assertNotIn(f"href='{url}'", html_content)
        self.assertNotIn("<a ", html_content)

    # 7. No Apply, Submit, Autofill, Upload, or Execute controls are generated
    @preserve_project_data_test
    def test_no_actionable_controls_generated(self):
        html_content = render_preview_html()
        # No buttons or forms
        self.assertNotIn("<button", html_content)
        self.assertNotIn("<form", html_content)
        # No actionable input types
        self.assertNotIn('type="submit"', html_content)
        self.assertNotIn('type="button"', html_content)
        self.assertNotIn('type="file"', html_content)
        self.assertNotIn('type="reset"', html_content)
        # Any inputs present must only be disabled checkboxes for the static review checklist
        for input_tag in re.findall(r"<input[^>]+>", html_content):
            self.assertIn('type="checkbox"', input_tag)
            self.assertIn("disabled", input_tag)

    # 8. No scripts, external resources, or network-request logic
    @preserve_project_data_test
    def test_no_scripts_or_external_resources(self):
        html_content = render_preview_html()
        self.assertNotIn("<script", html_content.lower())
        self.assertNotIn("<iframe", html_content.lower())
        self.assertNotIn("<link rel=\"stylesheet\"", html_content.lower())
        self.assertNotIn("@import", html_content.lower())
        self.assertNotIn("fetch(", html_content)
        self.assertNotIn("xmlhttprequest", html_content.lower())
        self.assertNotIn("websocket", html_content.lower())

    # 9. HTML escaping works correctly
    @preserve_project_data_test
    def test_html_escaping_works(self):
        html_content = render_preview_html()
        # Check that characters like <, >, & in user content are properly escaped
        self.assertNotIn("fullName <script>", html_content)
        self.assertIn("&gt;", html_content)

    # 10. Repeated generation produces deterministic output
    @preserve_project_data_test
    def test_deterministic_output(self):
        run1 = render_preview_html()
        run2 = render_preview_html()
        self.assertEqual(run1, run2)

    # 11. Artifact file writing works and file exists
    @preserve_project_data_test
    def test_artifact_file_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "test_preview.html"
            written = generate_preview_file(temp_path)
            self.assertTrue(written.exists())
            self.assertEqual(written.read_text(encoding="utf-8"), render_preview_html())
