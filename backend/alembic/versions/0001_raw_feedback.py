"""Initial immutable raw feedback storage."""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "feedbacks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(100)),
        sa.Column("user_id", sa.String(200)),
        sa.Column("user_type", sa.String(100)),
        sa.Column("feedback_created_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "external_id", name="uq_feedback_project_external"),
    )
    op.create_index("ix_feedbacks_project_id", "feedbacks", ["project_id"])


def downgrade():
    op.drop_index("ix_feedbacks_project_id", table_name="feedbacks")
    op.drop_table("feedbacks")
    op.drop_table("projects")
