"""Phase 2 — feedback_embeddings, clusters, cluster_members, cluster_reviews.

The vector column is stored as ``JSON`` so the migration is portable across
PostgreSQL and SQLite. Cosine similarity is computed in Python after
deserialising the list, which is sufficient for the demo dataset size and
keeps the unit tests runnable on SQLite without requiring pgvector. The
``embedding_dimension`` column is non-null and app-checked, so a wrong
dimension is rejected before any write.

If a future deployment wants server-side pgvector similarity search, a
follow-up migration can swap the JSON column for ``vector(N)`` on PostgreSQL.
The decision is recorded in ``docs/08-决策记录与修正.md``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


EMBEDDING_DIMENSION_HINT = 128


def upgrade() -> None:
    op.create_table(
        "feedback_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("feedback_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["feedback_analysis_id"],
            ["feedback_analyses.id"],
            name="fk_feedback_embedding_analysis",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["analysis_runs.id"],
            name="fk_feedback_embedding_run",
        ),
        sa.UniqueConstraint(
            "feedback_analysis_id",
            "embedding_model",
            name="uq_feedback_embedding_analysis_model",
        ),
        sa.CheckConstraint(
            "embedding_dimension > 0",
            name="ck_feedback_embedding_dimension_positive",
        ),
    )
    op.create_index(
        "ix_feedback_embeddings_run_id", "feedback_embeddings", ["run_id"]
    )
    op.create_index(
        "ix_feedback_embeddings_model", "feedback_embeddings", ["embedding_model"]
    )

    op.create_table(
        "clusters",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="algorithmic"),
        sa.Column("parent_cluster_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(200)),
        sa.Column("summary", sa.Text()),
        sa.Column("feedback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "unique_user_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("average_severity", sa.Float()),
        sa.Column("negative_ratio", sa.Float()),
        sa.Column("confidence", sa.Float()),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("distance_threshold", sa.Float(), nullable=False),
        sa.Column("linkage", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_clusters_project"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["analysis_runs.id"], name="fk_clusters_run"
        ),
        sa.ForeignKeyConstraint(
            ["parent_cluster_id"],
            ["clusters.id"],
            name="fk_clusters_parent",
        ),
        sa.CheckConstraint(
            "feedback_count >= 0 AND unique_user_count >= 0",
            name="ck_clusters_non_negative_counts",
        ),
        sa.CheckConstraint(
            "average_severity IS NULL OR average_severity BETWEEN 1 AND 5",
            name="ck_clusters_average_severity_range",
        ),
        sa.CheckConstraint(
            "negative_ratio IS NULL OR (negative_ratio >= 0 AND negative_ratio <= 1)",
            name="ck_clusters_negative_ratio_range",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_clusters_confidence_range",
        ),
    )
    op.create_index("ix_clusters_project_id", "clusters", ["project_id"])
    op.create_index("ix_clusters_run_id", "clusters", ["run_id"])

    op.create_table(
        "cluster_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("feedback_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["clusters.id"],
            name="fk_cluster_members_cluster",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_analysis_id"],
            ["feedback_analyses.id"],
            name="fk_cluster_members_analysis",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "cluster_id",
            "feedback_analysis_id",
            name="uq_cluster_member_cluster_analysis",
        ),
        sa.CheckConstraint(
            "similarity_score >= 0 AND similarity_score <= 1",
            name="ck_cluster_member_similarity_range",
        ),
    )
    op.create_index(
        "ix_cluster_members_cluster_id", "cluster_members", ["cluster_id"]
    )
    op.create_index(
        "ix_cluster_members_feedback_analysis_id",
        "cluster_members",
        ["feedback_analysis_id"],
    )

    op.create_table(
        "cluster_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["analysis_runs.id"], name="fk_cluster_reviews_run"
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["clusters.id"], name="fk_cluster_reviews_cluster"
        ),
        sa.UniqueConstraint(
            "cluster_id",
            "prompt_version",
            name="uq_cluster_review_cluster_prompt",
        ),
    )
    op.create_index(
        "ix_cluster_reviews_run_id", "cluster_reviews", ["run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_cluster_reviews_run_id", table_name="cluster_reviews")
    op.drop_table("cluster_reviews")

    op.drop_index(
        "ix_cluster_members_feedback_analysis_id", table_name="cluster_members"
    )
    op.drop_index("ix_cluster_members_cluster_id", table_name="cluster_members")
    op.drop_table("cluster_members")

    op.drop_index("ix_clusters_run_id", table_name="clusters")
    op.drop_index("ix_clusters_project_id", table_name="clusters")
    op.drop_table("clusters")

    op.drop_index("ix_feedback_embeddings_model", table_name="feedback_embeddings")
    op.drop_index("ix_feedback_embeddings_run_id", table_name="feedback_embeddings")
    op.drop_table("feedback_embeddings")
