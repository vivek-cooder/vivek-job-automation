"""Opt-in AI fallback for unfamiliar forms; deterministic Phase 2 stays primary."""

from worker.ai.browser_agent import BrowserAgent
from worker.ai.provider import create_provider
from worker.ai.settings import AISettings

_disabled_logged = False


def create_browser_agent(job_id=None):
    global _disabled_logged
    settings = AISettings.from_environment()
    if not settings.enabled:
        if not _disabled_logged:
            print("[AI] Fallback disabled (AI_AGENT_ENABLED=false).")
            _disabled_logged = True
        return None
    provider = create_provider(settings)
    return BrowserAgent(provider, settings, job_id=job_id)


__all__ = ["AISettings", "BrowserAgent", "create_browser_agent"]
