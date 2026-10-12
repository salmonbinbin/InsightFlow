from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = (
        "postgresql+psycopg://insightflow:insightflow_local@localhost:5433/insightflow"
    )

    # Phase 1 — AI Feedback Classification.
    # Keys are read from environment only; the secret is never logged or persisted.
    llm_api_key: Optional[str] = Field(default=None)
    llm_base_url: Optional[str] = Field(default=None)
    llm_model: Optional[str] = Field(default=None)
    llm_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    classification_batch_size: int = Field(default=20, ge=1, le=200)
    classification_max_retries: int = Field(default=2, ge=0, le=5)
    classification_prompt_version: str = Field(default="feedback-classification-v1")

    # Phase 2 — Embedding + Semantic Clustering.
    # ``embedding_model`` is the canonical name persisted on every embedding
    # row so vectors produced by different models are never mixed. Dimension is
    # fixed at migration time and must match the model's actual output length.
    # Real semantic embedding is enabled when both keys are set (env or
    # ``backend/.env``); otherwise the deterministic local hash embedder is
    # used so the pipeline can still run end-to-end.
    embedding_api_key: Optional[str] = Field(default=None)
    embedding_base_url: Optional[str] = Field(default=None)
    embedding_model: str = Field(default="local-hash-v1")
    embedding_dimension: int = Field(default=128, ge=8, le=4096)
    embedding_max_retries: int = Field(default=2, ge=0, le=5)
    embedding_max_batch_size: int = Field(default=64, ge=1, le=200)
    cluster_distance_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    cluster_linkage: str = Field(default="average")
    cluster_review_enabled: bool = Field(default=True)
    cluster_review_max_retries: int = Field(default=2, ge=0, le=5)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
