"""Load the user's local applicant profile without embedding personal data in code."""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APPLICANT_FILE = BASE_DIR / "profile" / "applicant.json"


def load_applicant_profile(path=APPLICANT_FILE):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Applicant profile not found at {path}; create it with only verified information."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Applicant profile must contain a JSON object.")
    return data
