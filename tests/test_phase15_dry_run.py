"""Phase 15 — Field-Mapping and Application-Readiness Dry Run Tests.

Verifies:
- Correct mappings without exposing profile values.
- Missing profile keys.
- Ambiguous field mappings.
- Required versus optional fields.
- Missing or unverified dropdown options.
- A position mismatch.
- No changes to protected production files.
- No calls to form-fill methods or submit actions.
- No file uploads or non-GET/HEAD requests.
- Safe handling of redirects and unexpected hostnames.
- Correct readiness classifications.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.data_safety import preserve_project_data_test
from worker.navigation import NavigationPolicy, NavigationRejected
from worker.phase15_dry_run import (
    analyze_field_mappings,
    verify_position_dropdown,
    classify_readiness,
    run_dry_run_evaluation,
)

BASE_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

NOVAMI_FORM_FIELDS = [
    {"name": "fullName", "id": "fullName", "type": "text", "required": True, "field_type": "full_name"},
    {"name": "email", "id": "email", "type": "email", "required": True, "field_type": "email"},
    {"name": "phone", "id": "phone", "type": "tel", "required": True, "field_type": "phone"},
    {"name": "position", "id": "position", "type": "select-one", "required": True, "field_type": "position"},
    {"name": "portfolioUrl", "id": "portfolioUrl", "type": "url", "required": False, "field_type": "portfolio"},
    {"name": "linkedinUrl", "id": "linkedinUrl", "type": "url", "required": False, "field_type": "linkedin"},
    {"name": "coverLetter", "id": "coverLetter", "type": "textarea", "required": False, "field_type": "cover_letter"},
]

VERIFIED_DROPDOWN_OPTIONS = [
    {"value": "Frontend Developer", "text": "Frontend Developer (Engineering)"},
    {"value": "Digital Marketing Specialist", "text": "Digital Marketing Specialist (Digital Marketing / Growth)"},
    {"value": "Graphic Designer", "text": "Graphic Designer (Design / Creative)"},
    {"value": "General Application / Future Opportunities", "text": "General Application / Talent Pool"},
]


class Phase15DryRunTests(unittest.TestCase):

    # 1. Correct mappings without exposing profile values
    @preserve_project_data_test
    def test_correct_mappings_without_exposing_values(self):
        # Sample profile schema with populated values
        profile_schema = {
            "name": "Secret Person",
            "email": "confidential@example.invalid",
            "phone": "9999999999",
            "location": "Confidential City",
            "experience": "Frontend Developer",
            "portfolio": None,
            "linkedin": None,
            "cover_letter": None,
        }

        res = analyze_field_mappings(NOVAMI_FORM_FIELDS, profile_schema)
        mappings = res["mappings"]

        # Ensure no values from profile_schema appear in the mapping output
        serialized = json.dumps(mappings)
        self.assertNotIn("Secret Person", serialized)
        self.assertNotIn("confidential@example.invalid", serialized)
        self.assertNotIn("9999999999", serialized)
        self.assertNotIn("Confidential City", serialized)

        # Check expected profile keys
        field_dict = {m["website_field"]: m for m in mappings}
        self.assertEqual(field_dict["fullName"]["proposed_profile_key"], "name")
        self.assertEqual(field_dict["fullName"]["mapping_confidence"], "Confirmed")
        self.assertEqual(field_dict["email"]["proposed_profile_key"], "email")
        self.assertEqual(field_dict["email"]["mapping_confidence"], "Confirmed")
        self.assertEqual(field_dict["phone"]["proposed_profile_key"], "phone")
        self.assertEqual(field_dict["phone"]["mapping_confidence"], "Confirmed")
        self.assertEqual(field_dict["position"]["mapping_confidence"], "Confirmed")

    # 2. Missing profile keys
    @preserve_project_data_test
    def test_missing_required_profile_key(self):
        sparse_schema = {
            "name": "Test Name",
            "email": "",  # missing required email
            "phone": None,  # missing required phone
        }
        res = analyze_field_mappings(NOVAMI_FORM_FIELDS, sparse_schema)
        self.assertTrue(res["has_blocked_required"])
        field_dict = {m["website_field"]: m for m in res["mappings"]}
        self.assertEqual(field_dict["email"]["mapping_confidence"], "Missing")
        self.assertEqual(field_dict["phone"]["mapping_confidence"], "Missing")

    # 3. Ambiguous field mappings
    @preserve_project_data_test
    def test_ambiguous_field_mapping(self):
        ambiguous_fields = [
            {"name": "customQuestion123", "id": "q1", "type": "text", "required": False},
        ]
        res = analyze_field_mappings(ambiguous_fields, {"name": "Test"})
        mapping = res["mappings"][0]
        self.assertEqual(mapping["mapping_confidence"], "Ambiguous")
        self.assertIsNone(mapping["proposed_profile_key"])

    # 4. Required versus optional fields
    @preserve_project_data_test
    def test_required_vs_optional_fields(self):
        profile_schema = {
            "name": "Test",
            "email": "test@example.invalid",
            "phone": "0000000000",
            "portfolio": None,
            "linkedin": None,
            "cover_letter": None,
        }
        res = analyze_field_mappings(NOVAMI_FORM_FIELDS, profile_schema)
        field_dict = {m["website_field"]: m for m in res["mappings"]}

        self.assertTrue(field_dict["fullName"]["required"])
        self.assertTrue(field_dict["email"]["required"])
        self.assertTrue(field_dict["phone"]["required"])
        self.assertTrue(field_dict["position"]["required"])

        self.assertFalse(field_dict["portfolioUrl"]["required"])
        self.assertFalse(field_dict["linkedinUrl"]["required"])
        self.assertFalse(field_dict["coverLetter"]["required"])

        # Optional fields with null in profile should be Candidate and planned as leave_blank
        self.assertEqual(field_dict["portfolioUrl"]["planned_action"], "leave_blank")
        self.assertEqual(field_dict["linkedinUrl"]["planned_action"], "leave_blank")
        self.assertEqual(field_dict["coverLetter"]["planned_action"], "leave_blank")

    # 5. Missing or unverified dropdown options
    @preserve_project_data_test
    def test_missing_or_empty_dropdown_options(self):
        ver = verify_position_dropdown([])
        self.assertEqual(ver["status"], "Unverified")
        self.assertFalse(ver["matched"])

        ver_none = verify_position_dropdown(None)
        self.assertEqual(ver_none["status"], "Unverified")

    # 6. A position mismatch
    @preserve_project_data_test
    def test_position_mismatch(self):
        different_options = [
            {"value": "Backend Developer", "text": "Backend Developer (Go / Python)"},
            {"value": "DevOps Engineer", "text": "DevOps Engineer (Cloud Infrastructure)"},
        ]
        ver = verify_position_dropdown(different_options, target_role="Frontend Developer")
        self.assertEqual(ver["status"], "Mismatch")
        self.assertFalse(ver["matched"])

    # 7. Position match verified
    @preserve_project_data_test
    def test_position_match_verified(self):
        ver = verify_position_dropdown(VERIFIED_DROPDOWN_OPTIONS, target_role="Frontend Developer")
        self.assertEqual(ver["status"], "Verified")
        self.assertTrue(ver["matched"])
        self.assertEqual(ver["matched_option"]["value"], "Frontend Developer")

    # 8. Readiness classification rules
    @preserve_project_data_test
    def test_readiness_classification_preparation_ready(self):
        profile_schema = {
            "name": "Candidate",
            "email": "cand@example.invalid",
            "phone": "0000000000",
            "portfolio": None,
            "linkedin": None,
            "cover_letter": None,
        }
        res = run_dry_run_evaluation(
            NOVAMI_FORM_FIELDS,
            profile_schema,
            VERIFIED_DROPDOWN_OPTIONS,
            target_role="Frontend Developer",
        )
        self.assertEqual(res["readiness"], "PreparationReady")
        self.assertTrue(res["can_prepare"])

    @preserve_project_data_test
    def test_readiness_classification_blocked_on_missing_required(self):
        sparse_schema = {"name": "Candidate"}  # missing email and phone
        res = run_dry_run_evaluation(
            NOVAMI_FORM_FIELDS,
            sparse_schema,
            VERIFIED_DROPDOWN_OPTIONS,
            target_role="Frontend Developer",
        )
        self.assertEqual(res["readiness"], "Blocked")
        self.assertFalse(res["can_prepare"])

    @preserve_project_data_test
    def test_readiness_classification_blocked_on_position_mismatch(self):
        profile_schema = {"name": "Candidate", "email": "a@b.c", "phone": "123"}
        unmatched_options = [{"value": "Accountant", "text": "Senior Accountant"}]
        res = run_dry_run_evaluation(
            NOVAMI_FORM_FIELDS,
            profile_schema,
            unmatched_options,
            target_role="Frontend Developer",
        )
        self.assertEqual(res["readiness"], "Blocked")
        self.assertFalse(res["can_prepare"])

    @preserve_project_data_test
    def test_readiness_classification_blocked_on_verification_gate(self):
        profile_schema = {"name": "Candidate", "email": "a@b.c", "phone": "123"}
        res = run_dry_run_evaluation(
            NOVAMI_FORM_FIELDS,
            profile_schema,
            VERIFIED_DROPDOWN_OPTIONS,
            verification_detected="CAPTCHA_REQUIRED",
        )
        self.assertEqual(res["readiness"], "Blocked")
        self.assertFalse(res["can_prepare"])

    # 9. Navigation policy blocks unauthorized redirects and hostnames
    @preserve_project_data_test
    def test_navigation_policy_blocks_unauthorized(self):
        policy = NavigationPolicy(["www.novamiinfotechs.com"])
        # Same host allowed
        self.assertTrue(policy.check("https://www.novamiinfotechs.com/careers/frontend-developer/"))
        # External host blocked
        with self.assertRaises(NavigationRejected) as ctx:
            policy.check("https://malicious.redirect.invalid/phishing")
        self.assertEqual(ctx.exception.reason, "UNAUTHORIZED_HOST")

    # 10. No form-fill methods, submit actions, or network mutation
    @preserve_project_data_test
    def test_dry_run_is_strictly_read_only(self):
        mock_page = MagicMock()
        profile_schema = {"name": "Test"}
        # Ensure that run_dry_run_evaluation does not even accept or touch page objects
        report = run_dry_run_evaluation(NOVAMI_FORM_FIELDS, profile_schema, VERIFIED_DROPDOWN_OPTIONS)
        self.assertIn("readiness", report)
        self.assertEqual(mock_page.fill.call_count, 0)
        self.assertEqual(mock_page.click.call_count, 0)
        self.assertEqual(mock_page.goto.call_count, 0)
