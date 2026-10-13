"""Phase 15 — Field-Mapping and Application-Readiness Dry Run Module.

Analyzes website form fields against applicant profile schema keys,
verifies dropdown options, and classifies readiness without filling
inputs, clicking controls, or exposing personal data values.
"""

from typing import Any, Dict, List, Optional, Set, Union
from worker.form_adapter import APPLICANT_KEYS, normalize

FIELD_MAPPING_DEFAULTS = {
    "fullName": "name",
    "email": "email",
    "phone": "phone",
    "position": "position",
    "portfolioUrl": "portfolio",
    "linkedinUrl": "linkedin",
    "coverLetter": "cover_letter",
}


def analyze_field_mappings(
    form_fields: List[Dict[str, Any]],
    profile_schema: Union[Dict[str, Any], Set[str], List[str]],
) -> Dict[str, Any]:
    """Analyze form fields against profile keys.

    Evaluates keys only; never extracts, logs, or returns personal values.
    """
    if isinstance(profile_schema, dict):
        available_keys = set(profile_schema.keys())
        populated_keys = {
            k for k, v in profile_schema.items()
            if v is not None and str(v).strip()
        }
    else:
        available_keys = set(profile_schema)
        populated_keys = set(profile_schema)

    mappings = []
    has_blocked_required = False
    has_unconfirmed_required = False

    for field in form_fields:
        name = field.get("name") or field.get("id") or ""
        field_type = field.get("field_type") or field.get("type") or "text"
        is_required = bool(field.get("required"))
        control_type = field.get("type") or field.get("tag") or "text"

        # Determine proposed profile key
        proposed_key = None
        if name in FIELD_MAPPING_DEFAULTS:
            proposed_key = FIELD_MAPPING_DEFAULTS[name]
        elif field.get("field_type") in APPLICANT_KEYS:
            proposed_key = APPLICANT_KEYS[field["field_type"]]

        confidence = "Missing"
        user_confirmation_needed = False
        unmapped_reason = None
        planned_action = "leave_blank"

        if name == "position" or field.get("field_type") == "position":
            confidence = "Confirmed"
            planned_action = "select_target_role"
            unmapped_reason = None
        elif proposed_key:
            if proposed_key in populated_keys:
                confidence = "Confirmed"
                planned_action = "map_from_profile"
            elif proposed_key in available_keys:
                # Key exists in schema but is empty/null
                confidence = "Candidate"
                if is_required:
                    confidence = "Missing"
                    unmapped_reason = f"Profile key '{proposed_key}' is empty/null for required field"
                    has_blocked_required = True
                else:
                    unmapped_reason = f"Profile key '{proposed_key}' is empty/null; optional field remains blank"
                    user_confirmation_needed = True
            else:
                confidence = "Missing"
                unmapped_reason = f"Profile schema lacks key '{proposed_key}'"
                if is_required:
                    has_blocked_required = True
        else:
            confidence = "Ambiguous" if not is_required else "Missing"
            unmapped_reason = "No matching profile key recognized"
            if is_required:
                has_blocked_required = True

        # Special handling for coverLetter
        if name == "coverLetter" or field.get("field_type") == "cover_letter":
            if "cover_letter" in populated_keys:
                confidence = "Confirmed"
                planned_action = "map_from_profile"
                unmapped_reason = None
            else:
                confidence = "Candidate"
                planned_action = "leave_blank"
                unmapped_reason = "No approved cover letter source exists; optional field remains blank"
                user_confirmation_needed = True

        mappings.append({
            "website_field": name,
            "input_type": control_type,
            "required": is_required,
            "proposed_profile_key": proposed_key,
            "mapping_confidence": confidence,
            "user_confirmation_needed": user_confirmation_needed,
            "reason_to_remain_unmapped": unmapped_reason,
            "planned_action": planned_action,
        })

    return {
        "mappings": mappings,
        "has_blocked_required": has_blocked_required,
        "has_unconfirmed_required": has_unconfirmed_required,
    }


def verify_position_dropdown(
    dropdown_options: Optional[List[Dict[str, str]]],
    target_role: str = "Frontend Developer",
) -> Dict[str, Any]:
    """Verify whether target_role is represented in available dropdown options."""
    if not dropdown_options:
        return {
            "status": "Unverified",
            "matched": False,
            "matched_option": None,
            "reason": "Dropdown options list is empty or unavailable",
            "available_options": [],
        }

    target_norm = normalize(target_role)
    matched_option = None

    for opt in dropdown_options:
        val = opt.get("value", "")
        txt = opt.get("text") or opt.get("label", "")
        val_norm = normalize(val)
        txt_norm = normalize(txt)

        if val_norm == target_norm or txt_norm == target_norm:
            matched_option = opt
            break
        if target_norm in val_norm or target_norm in txt_norm:
            matched_option = opt
            break

    if matched_option:
        return {
            "status": "Verified",
            "matched": True,
            "matched_option": matched_option,
            "reason": f"Target role '{target_role}' matched option '{matched_option.get('text')}'",
            "available_options": dropdown_options,
        }

    return {
        "status": "Mismatch",
        "matched": False,
        "matched_option": None,
        "reason": f"Target role '{target_role}' not found among options",
        "available_options": dropdown_options,
    }


def classify_readiness(
    field_analysis: Dict[str, Any],
    dropdown_verification: Dict[str, Any],
    form_selection_status: str = "Selected",
    verification_detected: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify overall readiness according to Phase 15 rules."""
    reasons = []

    if form_selection_status != "Selected":
        return {
            "readiness": "Blocked",
            "reasons": [f"Application form not uniquely selected (status: {form_selection_status})"],
            "can_prepare": False,
        }

    if verification_detected:
        return {
            "readiness": "Blocked",
            "reasons": [f"Human verification gate active: {verification_detected}"],
            "can_prepare": False,
        }

    if field_analysis.get("has_blocked_required"):
        return {
            "readiness": "Blocked",
            "reasons": ["One or more required fields are missing, empty in profile, or ambiguous"],
            "can_prepare": False,
        }

    dropdown_status = dropdown_verification.get("status")
    if dropdown_status != "Verified":
        return {
            "readiness": "Blocked",
            "reasons": [f"Position dropdown verification failed ({dropdown_status}): {dropdown_verification.get('reason')}"],
            "can_prepare": False,
        }

    if field_analysis.get("has_unconfirmed_required"):
        return {
            "readiness": "NeedsUserConfirmation",
            "reasons": ["Required mappings are identified but need user confirmation"],
            "can_prepare": False,
        }

    reasons.append("All required fields mapped to populated profile keys")
    reasons.append("Intended position 'Frontend Developer' verified in dropdown")
    reasons.append("Optional fields with no data (portfolio, linkedin, cover letter) safely planned as blank")

    return {
        "readiness": "PreparationReady",
        "reasons": reasons,
        "can_prepare": True,
    }


def run_dry_run_evaluation(
    form_fields: List[Dict[str, Any]],
    profile_schema: Union[Dict[str, Any], Set[str]],
    dropdown_options: Optional[List[Dict[str, str]]],
    target_role: str = "Frontend Developer",
    form_selection_status: str = "Selected",
    verification_detected: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute complete dry run evaluation without touching live page or data."""
    field_analysis = analyze_field_mappings(form_fields, profile_schema)
    dropdown_res = verify_position_dropdown(dropdown_options, target_role=target_role)
    readiness_res = classify_readiness(
        field_analysis, dropdown_res,
        form_selection_status=form_selection_status,
        verification_detected=verification_detected,
    )

    return {
        "target_role": target_role,
        "field_analysis": field_analysis,
        "dropdown_verification": dropdown_res,
        "readiness": readiness_res["readiness"],
        "readiness_reasons": readiness_res["reasons"],
        "can_prepare": readiness_res["can_prepare"],
    }
