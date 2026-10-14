"""Phase 6 — Insight evaluation runner tests.

Coverage:

* Happy path: completed insight run with multiple evidence rows
  referencing live feedbacks in the same project → ``completed``
  EvaluationRun with deterministic ``evidence_existence_rate`` and
  three human-reviewed placeholder metrics.
* Zero evidence: a run with no ``InsightEvidence`` rows → the metric
  is reported NOT_MEASURED (``sample_count=0``, ``value=None``).
  The brief: "空 evidence 时 value NULL/NOT MEASURED, 不能伪造100%".
* Cross-project: an InsightRun belonging to a different project is
  rejected.
* Wrong project feedback reference: an evidence row whose
  ``feedback_id`` points to a row in another project is recorded as
  a failed case with ``error_type='validation_error'`` and
  ``notes='wrong_project'``.
* External-id mismatch: an evidence row whose
  ``feedback_external_id`` snapshot disagrees with the live
  ``Feedback.external_id`` is recorded as a failed case with
  ``error_type='validation_error'`` and
  ``notes='external_id_mismatch'``.
* Missing feedback: an evidence row whose referenced feedback was
  deleted (FK dangling) is recorded as a failed case with
  ``error_type='not_measured'`` and ``notes='missing_feedback'``.
* Non-terminal source status: pending / running runs are rejected
  with the same typed exception the Phase 1/2 runner raises.
* Unmeasured semantic metrics: the three human-reviewed placeholders
  carry ``value=None``, ``sample_count=0``,
  ``metric_kind='human_reviewed'`` and an explicit
  ``requires_human_review`` flag in ``details``. The runner does
  NOT generate any semantic number.
* Raw feedback text is NEVER persisted by default.
* Token / latency columns propagate from the source InsightRun;
  ``unknown_token_calls > 0`` triggers NULL on the eval run.

The tests run against a private in-memory SQLite engine (the
shared migration database that ``scripts/verify_postgres.py``
drives must NOT be touched here, for the same reason as
``tests/test_phase6_runner.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.evals.insight_runner import (
    DATASET_NAME_INSIGHT_DB_SNAPSHOT,
    EVALUATION_TYPE_INSIGHT,
    HUMAN_REVIEWED_METRICS,
    evaluate_insight_run,
)
from app.evals.metrics import PHASE6_CALCULATION_VERSION
from app.evals.runner import EvaluationRunnerSourceMismatchError
from app.models import (
    Cluster,
    Feedback,
    Insight,
    InsightEvidence,
    InsightRun,
    Project,
)
from app.models.evaluation import (
    EvaluationCase,
    EvaluationCaseSource,
    EvaluationMetric,
    EvaluationRun,
    EvaluationRunStatus,
)

# ---- Fixture ----------------------------------------------------------------


@pytest.fixture
def db():
    """Provide a Session bound to a private in-memory SQLite."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


# ---- Builders ---------------------------------------------------------------


@dataclass(frozen=True)
class _InsightFixture:
    project: Project
    other_project: Project
    feedbacks: list[Feedback]
    other_feedback: Feedback
    run: InsightRun
    insights: list[Insight]


def _seed_insight_run(
    db: Session,
    *,
    n_feedback: int = 3,
    feedback_contents: tuple[str, ...] = ("alpha text", "beta text", "gamma text"),
    insight_status: str = "needs_validation",
    run_status: str = "completed",
    unknown_token_calls: int = 0,
    unknown_latency: bool = False,
    input_tokens: int = 100,
    output_tokens: int = 50,
    latency_ms: int = 2000,
    attempt_count: int = 1,
) -> _InsightFixture:
    """Seed Project + other Project + Feedbacks + completed InsightRun.

    The seed satisfies the source ``insight_runs`` CHECK
    ``success_count + failed_count + needs_validation_count
    <= processed_count <= input_count`` and the ``insights`` CHECK
    (needs_validation rows skip the mandatory-field requirement).
    """
    project = Project(name="Phase6-insight", product_name="test")
    other_project = Project(name="Phase6-insight-other", product_name="test")
    db.add(project)
    db.add(other_project)
    db.flush()

    # Build classification + cluster runs so the InsightRun FKs
    # (``clustering_run_id``, ``classification_run_id``) are
    # satisfied. We do not need the actual classification outputs;
    # the FK is the only thing the seed enforces.
    from app.models import AnalysisRun

    classification_run = AnalysisRun(
        project_id=project.id,
        run_type="classification",
        model="test-model",
        prompt_version="classification-v1",
        status="completed",
        input_count=n_feedback,
        processed_count=n_feedback,
        success_count=n_feedback,
    )
    clustering_run = AnalysisRun(
        project_id=project.id,
        run_type="clustering",
        model="test-model",
        prompt_version="cluster-review-v1",
        status="completed",
        input_count=n_feedback,
        processed_count=n_feedback,
        success_count=n_feedback,
    )
    db.add(classification_run)
    db.add(clustering_run)
    db.flush()

    clusters: list[Cluster] = []
    for i in range(n_feedback):
        cluster = Cluster(
            project_id=project.id,
            run_id=clustering_run.id,
            embedding_model="local-hash-v1",
            distance_threshold=0.5,
            linkage="average",
            feedback_count=1,
            unique_user_count=1,
        )
        db.add(cluster)
        db.flush()
        clusters.append(cluster)

    feedbacks: list[Feedback] = []
    for i in range(n_feedback):
        ext_id = f"F{i + 1:03d}"
        fb = Feedback(
            project_id=project.id,
            external_id=ext_id,
            content=feedback_contents[i] if i < len(feedback_contents) else f"text {i}",
        )
        db.add(fb)
        db.flush()
        feedbacks.append(fb)

    other_feedback = Feedback(
        project_id=other_project.id,
        external_id="OTHER001",
        content="other project text",
    )
    db.add(other_feedback)
    db.flush()

    run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering_run.id,
        classification_run_id=classification_run.id,
        status=run_status,
        model="insight-model",
        prompt_version="insight-v1",
        input_count=n_feedback,
        processed_count=n_feedback,
        success_count=n_feedback,
        needs_validation_count=0,
        failed_count=0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unknown_token_calls=unknown_token_calls,
        latency_ms=0 if unknown_latency else latency_ms,
        attempt_count=0 if unknown_latency else attempt_count,
        retry_count=0,
    )
    db.add(run)
    db.flush()

    insights: list[Insight] = []
    for i, fb in enumerate(feedbacks):
        ins = Insight(
            run_id=run.id,
            project_id=project.id,
            cluster_id=clusters[i].id,
            title=f"insight {i}" if insight_status == "draft" else None,
            pain_point=f"pain {i}" if insight_status == "draft" else None,
            affected_segment=f"seg {i}" if insight_status == "draft" else None,
            insight=f"body {i}" if insight_status == "draft" else None,
            recommendation=f"rec {i}" if insight_status == "draft" else None,
            confidence=0.9 if insight_status == "draft" else None,
            status=insight_status,
            feedback_count=1,
            evidence_count=1,
            source_cluster_external_id=f"cluster_{i}",
            source_member_external_ids=[fb.external_id],
            model="insight-model",
            prompt_version="insight-v1",
        )
        db.add(ins)
        db.flush()
        insights.append(ins)

        ev = InsightEvidence(
            insight_id=ins.id,
            feedback_id=fb.id,
            evidence_type="representative",
            relevance_score=0.8,
            feedback_external_id=fb.external_id,
            feedback_project_id=project.id,
            feedback_classification_run_id=classification_run.id,
        )
        db.add(ev)
    db.commit()

    return _InsightFixture(
        project=project,
        other_project=other_project,
        feedbacks=feedbacks,
        other_feedback=other_feedback,
        run=run,
        insights=insights,
    )


# ---- Tests ------------------------------------------------------------------


def test_insight_create_and_complete(db: Session):
    fx = _seed_insight_run(db)
    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED.value
    assert run.evaluation_type == EVALUATION_TYPE_INSIGHT
    assert run.stage == "insight"
    assert run.calculation_version == PHASE6_CALCULATION_VERSION
    assert run.input_count == 3
    assert run.processed_count == 3
    assert run.success_count == 3
    assert run.failed_count == 0
    assert run.dataset_name == DATASET_NAME_INSIGHT_DB_SNAPSHOT
    assert run.dataset_provenance == "unverified"
    assert run.dataset_split == "full"
    assert run.dataset_label_status == "db_snapshot"
    assert run.model == "insight-model"
    assert run.prompt_version == "insight-v1"
    assert run.input_tokens == 100
    assert run.output_tokens == 50
    assert run.latency_ms == 2000
    assert run.unknown_token_calls == 0
    notes = run.notes or ""
    assert "evaluation_type=insight_metrics" in notes
    assert "source_run_status=completed" in notes
    assert "dataset_source=db_snapshot" in notes
    assert "store_raw=False" in notes

    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
    ).all()
    by_name = {m.metric_name: m for m in metric_rows}
    # 1 deterministic + 3 human-reviewed = 4
    assert sorted(by_name) == sorted(["evidence_existence_rate", *HUMAN_REVIEWED_METRICS])
    existence = by_name["evidence_existence_rate"]
    assert existence.stage == "insight"
    assert existence.metric_kind == "deterministic"
    assert existence.sample_count == 3
    assert existence.value == pytest.approx(1.0)
    details = dict(existence.details)
    assert details["source_run_id"] == str(fx.run.id)
    assert details["source_run_status"] == "completed"
    assert details["total_evidence_rows"] == 3
    assert details["missing"] == []

    case_rows = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    assert len(case_rows) == 3
    for c in case_rows:
        assert c.project_id == fx.project.id
        assert c.stage == "insight"
        assert c.eval_source == EvaluationCaseSource.DB_RECORD.value
        assert c.passed is True
        assert c.error_type is None
        assert c.notes == "ok"
        assert c.input_hash
        assert c.expected_hash
        assert c.actual_hash
        # Raw feedback text MUST stay NULL.
        assert c.input is None
        assert c.expected is None
        assert c.actual is None
    # Mutually consistent counts.
    assert run.success_count + run.failed_count == run.processed_count
    assert run.processed_count <= run.input_count


def test_insight_zero_evidence_reports_not_measured(db: Session):
    """A run with no Insight rows emits NOT_MEASURED — never a fake 100%."""
    fx = _seed_insight_run(db, n_feedback=3)
    # No InsightEvidence rows attached — the helper attaches one per
    # feedback, so delete them all to exercise the empty path. We
    # use the ORM ``Table.delete()`` construct so the dialect binding
    # matches what the ORM uses on insert; raw ``text()`` is not used
    # so the test file does not need ``sqlalchemy.text``.
    db.execute(InsightEvidence.__table__.delete())
    db.execute(Insight.__table__.delete())
    db.commit()
    db.refresh(fx.run)

    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED.value
    assert run.input_count == 0
    assert run.processed_count == 0
    assert run.success_count == 0
    assert run.failed_count == 0

    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
    ).all()
    by_name = {m.metric_name: m for m in metric_rows}
    existence = by_name["evidence_existence_rate"]
    # NOT_MEASURED — value MUST be None, NOT a fabricated 1.0.
    assert existence.sample_count == 0
    assert existence.value is None
    assert existence.stage == "insight"
    assert existence.metric_kind == "deterministic"
    details = dict(existence.details)
    assert details["total_evidence_rows"] == 0
    assert details["referenced"] == 0
    assert details["total"] == 0

    # No per-evidence cases when there are no evidence rows.
    case_rows = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    assert case_rows == []


def test_insight_cross_project_rejected(db: Session):
    fx = _seed_insight_run(db)
    with pytest.raises(EvaluationRunnerSourceMismatchError) as exc_info:
        evaluate_insight_run(
            db,
            project_id=fx.other_project.id,
            insight_run_id=fx.run.id,
        )
    assert any("cross-project" in e for e in exc_info.value.errors)
    # No EvaluationRun row was written for either project.
    for project_id in (fx.project.id, fx.other_project.id):
        assert (
            db.scalar(select(EvaluationRun).where(EvaluationRun.project_id == project_id)) is None
        )


def test_insight_non_terminal_run_rejected(db: Session):
    fx = _seed_insight_run(db)
    # Bypass the ORM identity-map cache with a raw update so the
    # runner's ``db.refresh(run)`` is the only thing that surfaces
    # the new status to the validator.
    db.execute(
        InsightRun.__table__.update().where(InsightRun.id == fx.run.id).values(status="running")
    )
    db.commit()
    with pytest.raises(EvaluationRunnerSourceMismatchError) as exc_info:
        evaluate_insight_run(
            db,
            project_id=fx.project.id,
            insight_run_id=fx.run.id,
        )
    assert any("not terminal" in e for e in exc_info.value.errors)
    assert db.scalar(select(EvaluationRun).where(EvaluationRun.project_id == fx.project.id)) is None


def test_insight_wrong_project_feedback_reference(db: Session):
    """An evidence row pointing to a feedback in another project fails."""
    fx = _seed_insight_run(db, n_feedback=3)
    # Re-point one evidence row at the OTHER project's feedback. The
    # FK is still satisfied (the feedback exists) but the project
    # snapshot is now wrong. The runner must record this as a failed
    # case with ``error_type='validation_error'`` and
    # ``notes='wrong_project'``.
    target = fx.feedbacks[0]
    other_evidence = db.scalar(
        select(InsightEvidence).where(InsightEvidence.feedback_id == target.id)
    )
    db.execute(
        InsightEvidence.__table__.update()
        .where(InsightEvidence.id == other_evidence.id)
        .values(
            feedback_id=fx.other_feedback.id,
            feedback_external_id=fx.other_feedback.external_id,
        )
    )
    db.commit()

    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.PARTIAL_FAILED.value
    assert run.input_count == 3
    assert run.processed_count == 3
    assert run.success_count == 2
    assert run.failed_count == 1

    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    wrong = next(c for c in cases if str(c.case_id) == str(other_evidence.id))
    assert wrong.passed is False
    assert wrong.notes == "wrong_project"
    assert wrong.error_type == "validation_error"
    assert wrong.eval_source == EvaluationCaseSource.DB_RECORD.value
    assert wrong.input_hash and wrong.expected_hash and wrong.actual_hash

    # The evidence_existence_rate metric counts the cross-project
    # reference as missing (it is not in this project's known set).
    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
    ).all()
    by_name = {m.metric_name: m for m in metric_rows}
    existence = by_name["evidence_existence_rate"]
    assert existence.sample_count == 3
    assert existence.value == pytest.approx(2.0 / 3.0)
    details = dict(existence.details)
    assert str(fx.other_feedback.id) in details["missing"]


def test_insight_external_id_mismatch(db: Session):
    """An evidence row with a wrong ``feedback_external_id`` fails."""
    fx = _seed_insight_run(db, n_feedback=3)
    target = fx.feedbacks[0]
    other_evidence = db.scalar(
        select(InsightEvidence).where(InsightEvidence.feedback_id == target.id)
    )
    db.execute(
        InsightEvidence.__table__.update()
        .where(InsightEvidence.id == other_evidence.id)
        .values(feedback_external_id="TOTALLY-DIFFERENT-ID")
    )
    db.commit()

    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.PARTIAL_FAILED.value
    assert run.success_count == 2
    assert run.failed_count == 1

    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    mismatch = next(c for c in cases if str(c.case_id) == str(other_evidence.id))
    assert mismatch.passed is False
    assert mismatch.notes == "external_id_mismatch"
    assert mismatch.error_type == "validation_error"

    # The metric still sees the feedback row (it lives in this
    # project) so existence_rate stays at 1.0; only the per-case
    # decision captures the external_id mismatch.
    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
    ).all()
    by_name = {m.metric_name: m for m in metric_rows}
    existence = by_name["evidence_existence_rate"]
    assert existence.value == pytest.approx(1.0)
    assert existence.sample_count == 3


def test_insight_missing_feedback(db: Session):
    """An evidence row whose feedback was deleted is recorded as missing_feedback."""
    fx = _seed_insight_run(db, n_feedback=3)
    target = fx.feedbacks[0]
    target_evidence = db.scalar(
        select(InsightEvidence).where(InsightEvidence.feedback_id == target.id)
    )
    # Cache the textual id BEFORE the delete. The runner calls
    # ``db.expire_all()`` on entry, which evicts the identity-map
    # entry for ``target`` once the row is gone; any later attribute
    # access on the in-memory proxy then raises ``ObjectDeletedError``.
    # The post-eval assertion needs the missing id to appear in the
    # metric's ``missing`` list, so we capture the string form up
    # front and reuse the cached value below.
    target_id_str = str(target.id)
    # Bypass the SQLite FK enforcement to create a dangling FK —
    # the runner must NOT assume consistency. The integrity of the
    # source schema is a separate concern from what the runner
    # must tolerate in this evaluation pass. We use SQLAlchemy's
    # ``delete()`` construct (not raw ``text()``) so the UUID
    # parameter is bound through the same dialect path the ORM
    # uses on insert, otherwise the WHERE clause can fail to match
    # the stored UUID text and the DELETE silently no-ops. PRAGMA
    # itself is run via ``Connection.exec_driver_sql`` so the test
    # file does not pull ``sqlalchemy.text`` solely for the toggle.
    db.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
    db.execute(Feedback.__table__.delete().where(Feedback.id == target.id))
    db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    db.commit()

    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.PARTIAL_FAILED.value
    assert run.success_count == 2
    assert run.failed_count == 1

    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    missing = next(c for c in cases if str(c.case_id) == str(target_evidence.id))
    assert missing.passed is False
    assert missing.notes == "missing_feedback"
    assert missing.error_type == "not_measured"
    assert missing.eval_source == EvaluationCaseSource.DB_RECORD.value

    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
    ).all()
    by_name = {m.metric_name: m for m in metric_rows}
    existence = by_name["evidence_existence_rate"]
    assert existence.value == pytest.approx(2.0 / 3.0)
    assert target_id_str in dict(existence.details)["missing"]


def test_insight_human_reviewed_placeholders(db: Session):
    """The three human-reviewed metrics must carry NULL value / sample_count=0."""
    fx = _seed_insight_run(db)
    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
    ).all()
    by_name = {m.metric_name: m for m in metric_rows}
    assert set(HUMAN_REVIEWED_METRICS) <= set(by_name)
    for name in HUMAN_REVIEWED_METRICS:
        m = by_name[name]
        assert m.stage == "insight"
        assert m.metric_kind == "human_reviewed"
        assert m.value is None
        assert m.sample_count == 0
        details = dict(m.details)
        assert details["requires_human_review"] is True
        assert details["no_semantic_value_generated"] is True
        assert details["source_run_id"] == str(fx.run.id)
        assert details["source_run_status"] == "completed"
        # The reason explicitly says "human review" so a regression
        # cannot mistake the placeholder for a real measurement.
        assert "human" in details["reason"].lower()
        assert "no" in details["reason"].lower() or "not" in details["reason"].lower()


def test_insight_no_raw_feedback_persisted(db: Session):
    """By default EvaluationCase.input/expected/actual stay NULL."""
    fx = _seed_insight_run(db, n_feedback=2)
    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    case_rows = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    assert len(case_rows) == 2
    for c in case_rows:
        # Sensitive plaintext MUST stay NULL. The fingerprint hashes
        # are recorded so a regression comparison can detect drift.
        assert c.input is None
        assert c.expected is None
        assert c.actual is None
        assert c.input_hash
        assert c.expected_hash
        assert c.actual_hash


def test_insight_tokens_and_latency_propagate(db: Session):
    fx = _seed_insight_run(
        db,
        input_tokens=300,
        output_tokens=120,
        latency_ms=4500,
        attempt_count=4,
    )
    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    assert run.input_tokens == 300
    assert run.output_tokens == 120
    assert run.latency_ms == 4500
    assert run.attempt_count == 4
    assert run.unknown_token_calls == 0


def test_insight_unknown_token_calls_propagate(db: Session):
    fx = _seed_insight_run(
        db,
        unknown_token_calls=2,
        unknown_latency=True,
    )
    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    # unknown_token_calls > 0 → tokens stay NULL; unknown_latency
    # (attempt_count == 0) → latency_ms stays NULL. Both signals are
    # NOT_MEASURED on the eval row, never coerced to zero.
    assert run.input_tokens is None
    assert run.output_tokens is None
    assert run.latency_ms is None
    assert run.unknown_token_calls == 2


def test_insight_source_run_fixture_satisfies_check(db: Session):
    """The seed must satisfy ``ck_insight_runs_non_negative_counts``."""
    fx = _seed_insight_run(db)
    db.refresh(fx.run)
    assert fx.run.processed_count <= fx.run.input_count
    assert (
        fx.run.success_count + fx.run.failed_count + fx.run.needs_validation_count
        <= fx.run.processed_count
    )
    assert 0 <= fx.run.success_count
    assert 0 <= fx.run.failed_count
    assert 0 <= fx.run.needs_validation_count
    assert 0 <= fx.run.input_tokens
    assert 0 <= fx.run.output_tokens
    assert 0 <= fx.run.latency_ms
    assert 0 <= fx.run.attempt_count
    assert 0 <= fx.run.unknown_token_calls


def test_insight_constants_are_stable():
    """Public constants must remain stable for downstream tooling."""
    assert EVALUATION_TYPE_INSIGHT == "insight_metrics"
    assert DATASET_NAME_INSIGHT_DB_SNAPSHOT == "phase6_insight_db_snapshot"
    assert HUMAN_REVIEWED_METRICS == (
        "evidence_validity",
        "groundedness",
        "hallucination_rate",
    )
    assert PHASE6_CALCULATION_VERSION.startswith("phase6-")


def test_insight_provenance_is_unverified_not_synthetic(db: Session):
    """The runner does NOT masquerade the DB snapshot as ``synthetic``.

    A DB-snapshot run reads the live ``feedbacks`` rows; the runner
    has no signal whether those bytes are derived / synthetic /
    anonymised or production data. The persisted value MUST stay
    ``unverified`` until a separate provenance check promotes it —
    otherwise the row would silently vouch for the snapshot's
    certification.
    """
    fx = _seed_insight_run(db)
    run = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run)
    assert run.dataset_provenance == "unverified"
    assert run.dataset_provenance != "synthetic"
    notes = run.notes or ""
    assert "dataset_provenance=unverified" in notes
    # The runner writes an explicit disclaimer so an audit cannot
    # mistake the row for a verified-provenance evaluation.
    assert "db_source_unverified=True" in notes
    assert "provenance must be promoted" in notes.lower() or ("not certified" in notes.lower())


def test_insight_dataset_version_stable_for_same_snapshot(db: Session):
    """Re-evaluating the same source run yields identical version + hash.

    The version is derived from the content hash. Same evidence
    references, same identifiers, same referenced feedback content
    → identical SHA-256 → identical short version. The version MUST
    NOT be the source run UUID (the runner previously wrote
    ``source_run.id.hex`` here, which masked evidence / content drift).
    """
    fx = _seed_insight_run(db)
    run1 = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run1)
    run2 = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run2)
    assert run1.dataset_content_hash == run2.dataset_content_hash
    assert run1.dataset_version == run2.dataset_version
    # Version MUST NOT simply be the source-run id; that would mean
    # a re-run with drifted evidence could keep the same version.
    assert run1.dataset_version != fx.run.id.hex
    # Short version derived from the content hash; well under the
    # 64-char ``String(64)`` column budget.
    assert 0 < len(run1.dataset_version) <= 64
    # The version label also records the unverified provenance so a
    # downstream dashboard can grep for it.
    assert run1.dataset_version.startswith("unverified-")


def test_insight_dataset_version_and_hash_change_on_feedback_drift(
    db: Session,
):
    """Drift in feedback text changes version AND content_hash.

    The canonical payload includes the SHA-256 of every referenced
    ``Feedback.content``. A change in any referenced feedback's
    content therefore produces a new content hash and a new short
    version — so a regression comparison can detect content drift
    without persisting the raw feedback text.
    """
    fx = _seed_insight_run(db)
    run1 = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run1)
    baseline_hash = run1.dataset_content_hash
    baseline_version = run1.dataset_version

    # Drift one Feedback. The raw content is what the canonical
    # payload fingerprints, so the hash MUST change.
    target_id = fx.feedbacks[0].id
    db.execute(
        Feedback.__table__.update()
        .where(Feedback.id == target_id)
        .values(content="drifted content for traceability test")
    )
    db.commit()

    run2 = evaluate_insight_run(
        db,
        project_id=fx.project.id,
        insight_run_id=fx.run.id,
    )
    db.refresh(run2)

    assert run1.dataset_content_hash != run2.dataset_content_hash
    assert run1.dataset_version != run2.dataset_version
    assert run2.dataset_content_hash != baseline_hash
    assert run2.dataset_version != baseline_version
    # The provenance tag is independent of content drift. Even
    # though the version changed, the prefix stays ``unverified-``
    # so the short label continues to advertise that a separate
    # check must promote the snapshot's provenance.
    assert run2.dataset_version.startswith("unverified-")
