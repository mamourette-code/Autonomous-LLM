"""Provider registry.

To add a backend: implement the ``LLMProvider`` protocol in a module here and
add one branch to ``build_provider``.
"""

from __future__ import annotations

from autonomous.config import Settings
from autonomous.providers.base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolSpec,
)
from autonomous.providers.mock import MockProvider

AVAILABLE = ("gemini", "mock")


def build_provider(settings: Settings, name: str | None = None) -> LLMProvider:
    provider = (name or settings.provider).lower()
    if provider == "gemini":
        from autonomous.providers.gemini import GeminiProvider

        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    if provider == "mock":
        return MockProvider()
    raise ProviderError(f"unknown provider {provider!r}; available: {', '.join(AVAILABLE)}")


__all__ = [
    "AVAILABLE",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "MockProvider",
    "ProviderError",
    "ToolCall",
    "ToolSpec",
    "build_provider",
]
