"""Embedding-only adapters. Reference text never becomes executable instructions."""

import hashlib
import math
import re
from functools import lru_cache

import httpx

from app.core.config import get_settings


def validated(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise ValueError("Invalid embedding response")
    dimension = len(vectors[0]) if isinstance(vectors[0], list) else 0
    if not 1 <= dimension <= 2000:
        raise ValueError("Unsupported embedding dimension")
    result = []
    for values in vectors:
        if (
            not isinstance(values, list)
            or len(values) != dimension
            or any(
                not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v)
                for v in values
            )
        ):
            raise ValueError("Invalid embedding values")
        norm = math.sqrt(sum(v * v for v in values))
        if not norm:
            raise ValueError("Zero embedding")
        result.append([v / norm for v in values])
    return result


class FakeEmbedding:
    model_id = "fake:token-hash-v1"

    def embed(self, texts):
        vectors = []
        for value in texts:
            vector = [0.0] * 96
            for token in re.findall(r"[\w]+", value.lower()):
                vector[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 96] += 1
            if not any(vector):
                vector[0] = 1
            vectors.append(vector)
        return validated(vectors, len(texts))


class RemoteEmbedding:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(
            timeout=settings.knowledge_timeout_seconds, follow_redirects=False, transport=transport
        )
        self.hosted = settings.knowledge_embedding_provider == "openai-compatible"
        self.base = (
            settings.knowledge_embedding_url if self.hosted else settings.ollama_base_url
        ).rstrip("/")
        url = httpx.URL(self.base)
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Invalid configured embedding URL")
        self.model = (
            settings.knowledge_embedding_model if self.hosted else settings.ollama_embedding_model
        )
        if self.hosted:
            # Operators must change this revision when a hosted model alias changes.
            revision = settings.knowledge_embedding_revision
            if not revision:
                raise ValueError("Hosted embedding revision required")
        else:
            data = self.request("GET", "/api/tags")
            revision = next(
                (x["digest"] for x in data.get("models", []) if x.get("name") == self.model), None
            )
            if not revision:
                raise ValueError("Configured Ollama embedding model not installed")
        self.model_id = (
            ("hosted:" if self.hosted else "ollama:")
            + self.model
            + "@"
            + hashlib.sha256(revision.encode()).hexdigest()[:24]
        )
        if len(self.model_id) > 128:
            raise ValueError("Embedding model ID too long")

    def request(self, method, path, payload=None):
        headers = {}
        if self.hosted:
            key = self.settings.knowledge_embedding_api_key.get_secret_value()
            if not key:
                raise ValueError("Hosted embedding credential missing")
            headers["Authorization"] = "Bearer " + key
        try:
            with self.client.stream(
                method, self.base + path, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    body.extend(chunk)
                    if len(body) > 1048576:
                        raise ValueError("Embedding response too large")
                import json

                return json.loads(body)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            # Do not leak upstream URLs, headers, response bodies or credentials.
            raise ValueError("Embedding provider unavailable or invalid response") from None

    def embed(self, texts):
        if not 1 <= len(texts) <= 16 or any(len(x) > 4096 for x in texts):
            raise ValueError("Embedding input exceeds limits")
        if self.hosted:
            data = self.request("POST", "/embeddings", {"model": self.model, "input": texts})
            rows = sorted(data.get("data", []), key=lambda x: x["index"])
            if [x["index"] for x in rows] != list(range(len(texts))):
                raise ValueError("Invalid embedding indices")
            vectors = [x["embedding"] for x in rows]
        else:
            data = self.request(
                "POST", "/api/embed", {"model": self.model, "input": texts, "truncate": False}
            )
            vectors = data.get("embeddings")
        return validated(vectors, len(texts))


@lru_cache
def provider():
    settings = get_settings()
    return (
        FakeEmbedding()
        if settings.knowledge_embedding_provider == "fake"
        else RemoteEmbedding(settings)
    )
