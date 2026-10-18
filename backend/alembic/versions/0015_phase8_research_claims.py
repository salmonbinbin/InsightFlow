"""Keep structured facts and unverified analysis support separate; old rows stay NULL."""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("research_findings") as batch:
        batch.add_column(sa.Column("claim_kind", sa.String(20), nullable=True))
        batch.add_column(sa.Column("supporting_finding_ids", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("evidence_location", sa.JSON(), nullable=True))
        batch.create_check_constraint(
            "ck_research_findings_claim_kind",
            "claim_kind IS NULL OR claim_kind IN ('fact','inference','recommendation')",
        )
        batch.create_check_constraint(
            "ck_research_findings_analysis_unverified",
            "claim_kind IS NULL OR claim_kind = 'fact' OR "
            "(status = 'not_verified' AND supporting_finding_ids IS NOT NULL "
            "AND json_array_length(supporting_finding_ids) > 0)",
        )
        batch.create_check_constraint(
            "ck_research_findings_fact_location",
            "claim_kind IS NULL OR claim_kind <> 'fact' OR evidence_location IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("research_findings") as batch:
        batch.drop_constraint("ck_research_findings_fact_location", type_="check")
        batch.drop_constraint("ck_research_findings_analysis_unverified", type_="check")
        batch.drop_constraint("ck_research_findings_claim_kind", type_="check")
        batch.drop_column("evidence_location")
        batch.drop_column("supporting_finding_ids")
        batch.drop_column("claim_kind")
