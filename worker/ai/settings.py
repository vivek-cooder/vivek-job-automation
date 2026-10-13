"""Environment-only Phase 4 settings; AI is disabled unless explicitly enabled."""

import os
from dataclasses import dataclass


def _bool(name, default=False):
    raw = os.getenv(name)
    return default if raw is None else raw.strip().casefold() in {"1", "true", "yes", "on"}


def _number(name, default, low, high, cast):
    try:
        return min(high, max(low, cast(os.getenv(name, default))))
    except (TypeError, ValueError):
        return cast(default)


@dataclass(frozen=True)
class AISettings:
    enabled: bool = False
    provider: str = ""
    model: str = ""
    api_key: str = ""
    min_confidence: float = 0.90
    max_actions_per_job: int = 50
    max_page_analysis: int = 3
    max_retries: int = 0
    timeout_seconds: float = 20.0

    @classmethod
    def from_environment(cls):
        return cls(
            enabled=_bool("AI_AGENT_ENABLED", False),
            provider=os.getenv("AI_PROVIDER", "").strip().casefold(),
            model=os.getenv("AI_MODEL", "").strip(),
            api_key=os.getenv("AI_API_KEY", ""),
            min_confidence=_number("AI_MIN_CONFIDENCE", "0.90", 0.50, 1.0, float),
            max_actions_per_job=_number("AI_MAX_ACTIONS_PER_JOB", "50", 1, 100, int),
            max_page_analysis=_number("AI_MAX_PAGE_ANALYSIS", "3", 1, 10, int),
            max_retries=_number("AI_MAX_RETRIES", "0", 0, 2, int),
            timeout_seconds=_number("AI_TIMEOUT_SECONDS", "20", 3, 60, float),
        )
