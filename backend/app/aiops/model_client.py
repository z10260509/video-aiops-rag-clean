"""Configured bounded HTTP connection for hypothesis-model providers."""

import httpx


class ModelClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.hosted = settings.aiops_model_provider == "openai-compatible"
        self.name = settings.aiops_model_name or settings.ollama_chat_model
        self.model_id = ("hosted:" if self.hosted else "ollama:") + self.name
        self.base = (settings.aiops_model_url if self.hosted else settings.ollama_base_url).rstrip(
            "/"
        )
        url = httpx.URL(self.base)
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Invalid model URL")
        self.client = httpx.Client(
            timeout=min(180, max(1, settings.aiops_model_timeout_seconds)),
            follow_redirects=False,
            transport=transport,
        )
