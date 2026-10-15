from decimal import Decimal
from typing import Optional

from pydantic import Field, model_validator
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

    # Phase 4 — Opportunity Score + Human Decision.
    # Centralised default weights and Priority thresholds. Validation
    # runs at process start so an invalid default fails fast instead of
    # silently corrupting scores.
    opportunity_weight_user_coverage: Decimal = Field(default=Decimal("0.25"))
    opportunity_weight_severity: Decimal = Field(default=Decimal("0.25"))
    opportunity_weight_growth: Decimal = Field(default=Decimal("0.15"))
    opportunity_weight_user_value: Decimal = Field(default=Decimal("0.20"))
    opportunity_weight_business_value: Decimal = Field(default=Decimal("0.15"))
    opportunity_priority_p1_below: Decimal = Field(default=Decimal("5.00"))
    opportunity_priority_p0_below: Decimal = Field(default=Decimal("7.50"))
    opportunity_prompt_version: str = Field(default="opportunity-recommendation-v1")
    opportunity_max_retries: int = Field(default=2, ge=0, le=5)

    # Phase 5 — Requirement + PRD.
    # Prompt versions are pinned so historical rows remain reproducible;
    # retries only apply to the AI tool call itself (transport errors).
    requirement_prompt_version: str = Field(default="requirement-generation-v1")
    requirement_max_retries: int = Field(default=2, ge=0, le=5)
    prd_prompt_version: str = Field(default="prd-generation-v3")
    prd_max_retries: int = Field(default=2, ge=0, le=5)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _validate_phase4_defaults(self) -> "Settings":
        weights = [
            self.opportunity_weight_user_coverage,
            self.opportunity_weight_severity,
            self.opportunity_weight_growth,
            self.opportunity_weight_user_value,
            self.opportunity_weight_business_value,
        ]
        for weight in weights:
            if weight < Decimal("0") or weight > Decimal("1"):
                raise ValueError(f"opportunity default weights must be within [0, 1]; got {weight}")
        total = sum(weights)
        if abs(total - Decimal("1")) > Decimal("1e-9"):
            raise ValueError(f"opportunity default weights must sum to 1; got {total}")
        if not (
            Decimal("0")
            < self.opportunity_priority_p1_below
            < self.opportunity_priority_p0_below
            <= Decimal("10")
        ):
            raise ValueError(
                "opportunity priority thresholds must satisfy 0 < p1_below < p0_below <= 10"
            )
        return self


settings = Settings()
