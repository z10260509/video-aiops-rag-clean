from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://video:video@localhost:5432/video_platform"
    redis_url: str = "redis://localhost:6379/0"
    storage_root: Path = Path("storage")
    max_upload_bytes: int = 100 * 1024 * 1024
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "gemma4:e2b-it-qat"
    ollama_embedding_model: str = "embeddinggemma:300m"
    remediation_enabled: bool = False
    video_metrics_enabled: bool = False
    video_metrics_queue: Literal["celery"] = "celery"
    video_metrics_port: int = 9108
    aiops_enabled: bool = False
    aiops_operator_token: SecretStr = SecretStr("")
    aiops_webhook_token: SecretStr = SecretStr("")
    aiops_operator_principal: str = "local-operator"
    aiops_allowed_namespaces: str = "video-platform"
    knowledge_vectors_enabled: bool = False
    knowledge_embedding_provider: Literal["fake", "ollama", "openai-compatible"] = "fake"
    knowledge_embedding_url: str = "https://api.openai.com/v1"
    knowledge_embedding_model: str = ""
    knowledge_embedding_revision: str = ""
    knowledge_embedding_api_key: SecretStr = SecretStr("")
    knowledge_timeout_seconds: float = 30.0
    aiops_video_enabled: bool = False
    aiops_video_demo_enabled: bool = False
    aiops_video_reader_token: SecretStr = SecretStr("")
    aiops_video_repair_token: SecretStr = SecretStr("")
    aiops_video_scan_since: str = ""
    aiops_approval_enabled: bool = False
    aiops_approval_ttl_seconds: int = 600
    aiops_hypothesis_remediation_enabled: bool = False
    aiops_graph_enabled: bool = False
    aiops_model_provider: Literal["fake", "ollama", "openai-compatible"] = "fake"
    aiops_model_url: str = "https://api.openai.com/v1"
    aiops_model_name: str = ""
    aiops_model_api_key: SecretStr = SecretStr("")
    aiops_model_timeout_seconds: float = 30.0
    aiops_model_context_tokens: int = 8192
    aiops_tools_enabled: bool = False
    aiops_loki_url: str = "http://p1-loki.monitoring.svc.cluster.local:3100"
    aiops_tool_timeout_seconds: float = 3.0
    kubernetes_api_url: str | None = None
    kubernetes_bearer_token: str | None = None
    kubernetes_ca_file: str | None = None
    prometheus_url: str | None = None
    prometheus_bearer_token: str | None = None

    @property
    def input_dir(self) -> Path:
        return self.storage_root / "input"

    @property
    def output_dir(self) -> Path:
        return self.storage_root / "output"

    @property
    def thumbnail_dir(self) -> Path:
        return self.storage_root / "thumbnails"


@lru_cache
def get_settings() -> Settings:
    return Settings()
