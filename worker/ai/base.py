"""Provider contract and safe provider error categories."""

from abc import ABC, abstractmethod


class AIProviderError(Exception):
    def __init__(self, code):
        self.code = str(code or "AI_PROVIDER_ERROR")
        super().__init__(self.code)


class AIProvider(ABC):
    name = "abstract"

    @abstractmethod
    def analyze_page(self, page_snapshot):
        """Return a structured classification plan; never execute browser actions."""
        raise NotImplementedError
