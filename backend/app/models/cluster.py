"""Phase 2 — Semantic Clustering.

Four new tables track the clustering pipeline:

* :class:`FeedbackEmbedding` — one row per ``(feedback_analysis_id,
  embedding_model)`` pair. Vectors produced by different models are never
  compared.
* :class:`Cluster` — output of the algorithmic + LLM semantic review. Owned
  by a clustering run (``analysis_runs`` with ``run_type='clustering'``).
* :class:`ClusterMember` — feedback to cluster membership with the cosine
  similarity used for placement.
* :class:`ClusterReview` — audit log of every LLM review decision the
  runner applied, so the cluster topology is reproducible.

``feedbacks.content`` is never modified here; only new rows in new tables.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now
from app.db.vector import EmbeddingVector


class Embedding(Base):
    """A single embedding for one ``FeedbackAnalysis``.

    The ``UNIQUE(feedback_analysis_id, embedding_model)`` constraint guarantees
    that re-running the pipeline never produces duplicate vectors for the
    same model and that different models are stored as separate rows.
    """

    __tablename__ = "feedback_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "feedback_analysis_id",
            "embedding_model",
            name="uq_feedback_embedding_analysis_model",
        ),
        CheckConstraint(
            "embedding_dimension > 0",
            name="ck_feedback_embedding_dimension_positive",
        ),
        CheckConstraint(
            "vector_dims(embedding) = embedding_dimension",
            name="ck_feedback_embedding_actual_dimension",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("vector_norm(embedding) > 0", name="ck_feedback_embedding_nonzero").ddl_if(
            dialect="postgresql"
        ),
        Index("ix_feedback_embeddings_run_id", "run_id"),
        Index("ix_feedback_embeddings_model", "embedding_model"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    feedback_analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("feedback_analyses.id", ondelete="CASCADE"),
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding_dimension: Mapped[int] = mapped_column(Integer)
    # Variable-dimension pgvector with a database check against the recorded dimension.
    embedding: Mapped[list[float]] = mapped_column(EmbeddingVector())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClusterSource(str, Enum):
    ALGORITHMIC = "algorithmic"
    LLM_REVIEW = "llm_review"
    OUTLIER = "outlier"


class Cluster(Base):
    """Output cluster after algorithmic clustering and optional LLM review.

    ``name`` / ``summary`` / ``confidence`` may be filled by the LLM review
    step. They default to ``NULL`` so we never invent a name just to satisfy
    a schema.
    """

    __tablename__ = "clusters"
    __table_args__ = (
        CheckConstraint(
            "feedback_count >= 0 AND unique_user_count >= 0",
            name="ck_clusters_non_negative_counts",
        ),
        CheckConstraint(
            "average_severity IS NULL OR average_severity BETWEEN 1 AND 5",
            name="ck_clusters_average_severity_range",
        ),
        CheckConstraint(
            "negative_ratio IS NULL OR (negative_ratio >= 0 AND negative_ratio <= 1)",
            name="ck_clusters_negative_ratio_range",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_clusters_confidence_range",
        ),
        Index("ix_clusters_project_id", "project_id"),
        Index("ix_clusters_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))

    # When the LLM review splits a cluster, the new clusters point back to
    # the candidate they came from so the audit chain is preserved.
    source: Mapped[str] = mapped_column(String(20), default=ClusterSource.ALGORITHMIC.value)
    parent_cluster_id: Mapped[UUID | None] = mapped_column(ForeignKey("clusters.id"), nullable=True)

    name: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    feedback_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_user_count: Mapped[int] = mapped_column(Integer, default=0)
    average_severity: Mapped[float | None] = mapped_column(Float)
    negative_ratio: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)

    embedding_model: Mapped[str] = mapped_column(String(100))
    distance_threshold: Mapped[float] = mapped_column(Float)
    linkage: Mapped[str] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClusterMember(Base):
    """Membership row: which feedback analysis belongs to which cluster."""

    __tablename__ = "cluster_members"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id",
            "feedback_analysis_id",
            name="uq_cluster_member_cluster_analysis",
        ),
        CheckConstraint(
            "similarity_score >= 0 AND similarity_score <= 1",
            name="ck_cluster_member_similarity_range",
        ),
        Index("ix_cluster_members_cluster_id", "cluster_id"),
        Index("ix_cluster_members_feedback_analysis_id", "feedback_analysis_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"))
    feedback_analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("feedback_analyses.id", ondelete="CASCADE")
    )
    similarity_score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClusterReviewAction(str, Enum):
    KEEP = "keep"
    SPLIT = "split"
    MERGE = "merge"
    OUTLIER = "outlier"


class ClusterReview(Base):
    """Audit log of every LLM cluster review decision the runner applied.

    The runner writes one row per candidate cluster that was reviewed. The
    ``response_json`` column stores the raw structured payload so future
    audits can replay the decision without depending on the LLM vendor.
    """

    __tablename__ = "cluster_reviews"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id",
            "prompt_version",
            name="uq_cluster_review_cluster_prompt",
        ),
        Index("ix_cluster_reviews_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"))
    action: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(100))
    response_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


__all__ = [
    "Cluster",
    "ClusterMember",
    "ClusterReview",
    "ClusterReviewAction",
    "ClusterSource",
    "Embedding",
]
