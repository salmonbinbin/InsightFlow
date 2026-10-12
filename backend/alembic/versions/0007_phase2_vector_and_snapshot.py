"""Enable native pgvector and persist clustering input/configuration snapshots.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analysis_runs", sa.Column("cluster_config", sa.JSON(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # Preserve existing JSON vectors; invalid legacy values fail the migration atomically.
        op.execute(
            "ALTER TABLE feedback_embeddings ALTER COLUMN embedding TYPE vector USING embedding::text::vector"
        )
        op.create_check_constraint(
            "ck_feedback_embedding_actual_dimension",
            "feedback_embeddings",
            "vector_dims(embedding) = embedding_dimension",
        )
        op.create_check_constraint(
            "ck_feedback_embedding_nonzero", "feedback_embeddings", "vector_norm(embedding) > 0"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("ck_feedback_embedding_nonzero", "feedback_embeddings", type_="check")
        op.drop_constraint(
            "ck_feedback_embedding_actual_dimension", "feedback_embeddings", type_="check"
        )
        op.execute(
            "ALTER TABLE feedback_embeddings ALTER COLUMN embedding TYPE json USING embedding::text::json"
        )
        # Extension may be shared with other applications: do not drop it.
    op.drop_column("analysis_runs", "cluster_config")
