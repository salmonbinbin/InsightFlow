"""Phase 8 — Research Agent (external market intelligence).

Three new tables back the Research pipeline without touching any
internal Evidence surface:

* :class:`ResearchRun` — one row per research execution. The user-
  supplied ``research_question`` and ``scope`` are recorded verbatim
  (with their lengths) along with the assembled internal context
  summary (Insight title + representative Evidence excerpts + PM
  Decision snapshot). The run captures the model binding, the prompt
  version (``research-agent-v1``), five-state status, per-attempt
  token / latency / attempts / retry accounting and the safe error
  message. The internal context snapshot is persisted as a JSON hash
  so a future re-run can detect drift without re-reading the raw
  Evidence content.

* :class:`ResearchSource` — one row per external search hit the
  Tavily provider returned. URL / title / domain / snippet /
  ``provider_source_id`` / ``retrieved_at`` are all preserved so the
  Findings can cite the exact provider artefact. The URL is
  validated to be an http(s) URL on insert; non-conforming URLs
  raise a validation error and never reach the database.

* :class:`ResearchFinding` — the LLM's structured output broken down
  per ``category`` (``summary`` / ``competitor`` / ``market_pattern``
  / ``opportunity`` / ``risk``). Every factual finding carries a
  ``source_refs`` list that points at the ``ResearchSource`` rows
  produced by THIS run — server-side validation rejects any
  ``source_refs`` value that does not reference a persisted source
  row. ``confidence`` is constrained to [0, 1]. ``status`` is
  ``confirmed`` (has sources) or ``not_verified`` (no sources
  available or the LLM produced a non-grounded claim).

The internal Evidence table (``insight_evidence``) and the
``feedbacks`` table are never modified by this module — external
research and internal evidence live in separate stores per the long-
term plan (``docs/11`` §82).
"""

from __future__ import annotations

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


class ResearchRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


RESEARCH_RUN_STATUSES: tuple[str, ...] = tuple(s.value for s in ResearchRunStatus)


class ResearchFindingCategory(str, Enum):
    SUMMARY = "summary"
    COMPETITOR = "competitor"
    MARKET_PATTERN = "market_pattern"
    OPPORTUNITY = "opportunity"
    RISK = "risk"


RESEARCH_FINDING_CATEGORIES: tuple[str, ...] = tuple(c.value for c in ResearchFindingCategory)


class ResearchFindingStatus(str, Enum):
    CONFIRMED = "confirmed"
    NOT_VERIFIED = "not_verified"


RESEARCH_FINDING_STATUSES: tuple[str, ...] = tuple(s.value for s in ResearchFindingStatus)


class ResearchRun(Base):
    """One competitive / market research execution for a single Insight."""

    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(
            "length(research_question) BETWEEN 1 AND 500",
            name="ck_research_runs_question_length",
        ),
        CheckConstraint(
            "length(scope) <= 2000",
            name="ck_research_runs_scope_length",
        ),
        CheckConstraint(
            "length(provider) BETWEEN 1 AND 64",
            name="ck_research_runs_provider_length",
        ),
        CheckConstraint(
            "length(model) BETWEEN 1 AND 100 OR model IS NULL",
            name="ck_research_runs_model_length",
        ),
        CheckConstraint(
            "status IN ('pending','running','completed','partial_failed','failed')",
            name="ck_research_runs_status_allowed",
        ),
        CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 AND "
            "source_count >= 0 AND finding_count >= 0 AND "
            "input_tokens >= 0 AND output_tokens >= 0 AND "
            "latency_ms >= 0 AND attempt_count >= 0 AND "
            "retry_count >= 0 AND unknown_token_calls >= 0",
            name="ck_research_runs_non_negative_counts",
        ),
        CheckConstraint(
            "processed_count <= input_count",
            name="ck_research_runs_processed_le_input",
        ),
        Index("ix_research_runs_project_id", "project_id"),
        Index("ix_research_runs_insight_id", "insight_id"),
        Index("ix_research_runs_status", "status"),
        Index("ix_research_runs_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    insight_id: Mapped[UUID] = mapped_column(ForeignKey("insights.id"))
    classification_run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    clustering_run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    insight_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("insight_decisions.id"), nullable=True
    )

    # User-supplied inputs (length-bounded at the API; CHECK constraint
    # is a defence-in-depth so direct DB writes cannot violate the
    # contract).
    research_question: Mapped[str] = mapped_column(String(500))
    scope: Mapped[str | None] = mapped_column(Text)

    # Provider / model / prompt provenance. ``provider`` is the canonical
    # name of the Research Tool Provider (e.g. ``tavily``); ``query``
    # is the actual query string that was sent to the provider.
    provider: Mapped[str] = mapped_column(String(64))
    provider_query: Mapped[str] = mapped_column(String(500))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))

    status: Mapped[str] = mapped_column(String(20), default=ResearchRunStatus.PENDING.value)
    started_at: Mapped["__import__('datetime').datetime | None"] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped["__import__('datetime').datetime | None"] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped["__import__('datetime').datetime"] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    # Internal-context snapshot. The Insight title / representative
    # Evidence external IDs / PM Decision snapshot are serialised here
    # so a retry can re-use the original inputs without re-reading the
    # raw feedback content (which is the internal evidence surface).
    internal_context: Mapped[dict] = mapped_column(JSON)
    internal_context_hash: Mapped[str] = mapped_column(String(64))

    # Nullable for historical runs; new v5 executions retain the independent
    # task/coverage contract and bounded, public-source-only trace.
    task_contract: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    coverage_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Counters and model accounting.
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    unknown_token_calls: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class ResearchSource(Base):
    """A single search hit returned by the Research Tool Provider.

    The URL is validated to be a syntactically-valid http(s) URL on
    insert; ``domain`` is derived from the URL host and persisted so
    reads do not have to re-parse. ``provider_source_id`` is the
    provider's opaque identifier (``id`` for Tavily) so the same hit
    can be reconciled against a future retrieval without trusting the
    LLM to remember.
    """

    __tablename__ = "research_sources"
    __table_args__ = (
        CheckConstraint(
            "length(url) BETWEEN 1 AND 2048",
            name="ck_research_sources_url_length",
        ),
        CheckConstraint(
            "length(title) <= 500",
            name="ck_research_sources_title_length",
        ),
        CheckConstraint(
            "length(domain) BETWEEN 1 AND 253",
            name="ck_research_sources_domain_length",
        ),
        CheckConstraint(
            "length(provider_source_id) BETWEEN 1 AND 200",
            name="ck_research_sources_provider_id_length",
        ),
        CheckConstraint(
            "length(provider) BETWEEN 1 AND 64",
            name="ck_research_sources_provider_length",
        ),
        UniqueConstraint(
            "run_id",
            "provider_source_id",
            name="uq_research_sources_run_provider_id",
        ),
        Index("ix_research_sources_run_id", "run_id"),
        Index("ix_research_sources_domain", "domain"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(64))
    provider_source_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(500))
    domain: Mapped[str] = mapped_column(String(253))
    snippet: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped["__import__('datetime').datetime"] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    created_at: Mapped["__import__('datetime').datetime"] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class ResearchFinding(Base):
    """One structured research finding produced by the LLM.

    ``category`` matches the structured output schema; ``status`` is
    ``confirmed`` when the finding carries at least one valid
    ``source_refs`` entry pointing at a persisted ResearchSource, and
    ``not_verified`` otherwise. The two CHECK constraints make this
    invariant enforceable at the DB layer so a buggy caller cannot
    persist a contradictory state.
    """

    __tablename__ = "research_findings"
    __table_args__ = (
        CheckConstraint(
            "claim_kind IS NULL OR claim_kind IN ('fact','inference','recommendation')",
            name="ck_research_findings_claim_kind",
        ),
        CheckConstraint(
            "claim_kind IS NULL OR claim_kind = 'fact' OR "
            "(status = 'not_verified' AND supporting_finding_ids IS NOT NULL "
            "AND json_array_length(supporting_finding_ids) > 0)",
            name="ck_research_findings_analysis_unverified",
        ),
        CheckConstraint(
            "claim_kind IS NULL OR claim_kind <> 'fact' OR evidence_location IS NOT NULL",
            name="ck_research_findings_fact_location",
        ),
        CheckConstraint(
            "category IN ('summary','competitor','market_pattern','opportunity','risk')",
            name="ck_research_findings_category_allowed",
        ),
        CheckConstraint(
            "status IN ('confirmed','not_verified')",
            name="ck_research_findings_status_allowed",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_research_findings_confidence_range",
        ),
        CheckConstraint(
            "length(title) BETWEEN 1 AND 200",
            name="ck_research_findings_title_length",
        ),
        CheckConstraint(
            "length(description) BETWEEN 1 AND 4000",
            name="ck_research_findings_description_length",
        ),
        CheckConstraint(
            "((status = 'confirmed') "
            "AND (source_refs IS NOT NULL AND json_array_length(source_refs) > 0)) "
            "OR "
            "((status = 'not_verified') "
            "AND (source_refs IS NULL OR json_array_length(source_refs) = 0))",
            name="ck_research_findings_status_provided_invariant",
        ),
        Index("ix_research_findings_run_id", "run_id"),
        Index("ix_research_findings_category", "category"),
        Index("ix_research_findings_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(20), default=ResearchFindingStatus.NOT_VERIFIED.value
    )
    # JSON array of ``{"source_id": int, "url": str}`` entries that
    # point at the run's persisted ResearchSource rows. ``jsonb_array_length``
    # is dialect-neutral on SQLite/PostgreSQL when the column type is JSON.
    # ``none_as_null=True`` ensures Python ``None`` is persisted as SQL
    # NULL (not JSON ``null``); the CHECK constraint
    # ``ck_research_findings_status_provided_invariant`` then evaluates
    # cleanly on PostgreSQL because ``json_array_length(NULL)`` returns
    # NULL (not a DataError on JSON null).
    source_refs: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    # NULL identifies historical rows. Analysis support UUIDs are rebound from
    # confirmed facts in the same Run/project before insertion, never model IDs.
    claim_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    supporting_finding_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    evidence_location: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped["__import__('datetime').datetime"] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


__all__ = [
    "RESEARCH_FINDING_CATEGORIES",
    "RESEARCH_FINDING_STATUSES",
    "RESEARCH_RUN_STATUSES",
    "ResearchFinding",
    "ResearchFindingCategory",
    "ResearchFindingStatus",
    "ResearchRun",
    "ResearchRunStatus",
    "ResearchSource",
]
