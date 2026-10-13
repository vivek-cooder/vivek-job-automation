"""Small local configuration helpers. Missing/invalid submission flags fail closed."""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config(path=CONFIG_FILE):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}


def auto_submit_enabled(path=CONFIG_FILE):
    return load_config(path).get("auto_submit") is True


def resolve_auto_submit(auto_submit=None, path=CONFIG_FILE):
    return auto_submit_enabled(path) if auto_submit is None else (auto_submit is True)
