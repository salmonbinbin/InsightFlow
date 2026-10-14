"""Phase 6 — evaluation runner service tests.

The tests run against a private in-memory SQLite engine. They do NOT
touch the shared Postgres database that ``scripts/verify_postgres.py``
drives, so the Alembic migrations and ``alembic_version`` row remain
intact for the verification script's ``alembic downgrade base`` step.
Real Postgres validation of the runner is left to a dedicated,
migration-fresh database that Codex runs separately.

Coverage:

* Happy path: classification run + matching dataset → ``completed``
  EvaluationRun with deterministic metrics and per-case hashes.
* Partial path: at least one dataset case lacks a prediction →
  ``partial_failed``, ``no_prediction`` case row recorded.
* Failure path: an internal exception → ONE ``failed`` EvaluationRun
  with the error message; no fake-completed row left behind.
* Source validation: cross-project, wrong run_type, non-terminal
  status are rejected BEFORE any DB write.
* Dataset validation: content hash mismatch (Feedback.content drifted
  from dataset.feedback_text) is rejected.
* Cluster path: purity / coverage / fragmentation / over-merge /
  count are computed; per-case rows record ``predicted_cluster`` /
  ``gt_cluster``; duplicate external_id is diagnosed; the
  ``local-hash-v1`` embedding model triggers an explicit
  "NOT real semantic embedding" warning.
* Counts stay mutually consistent: input_count, processed_count,
  success_count + failed_count, and EvaluationMetric rows all line up.
* Token / latency columns propagate from the source run; ``None``
  becomes ``NULL`` and ``unknown_token_calls`` is preserved.
* Raw case text is opt-in: by default EvaluationCase.input stays
  NULL; with ``store_raw=True`` it carries the dataset's
  ``feedback_text``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.evals.dataset import (
    DATASET_SPLIT_FULL,
    DatasetCase,
    DatasetProvenance,
    build_dataset_version,
    hash_dataset_cases,
)
from app.evals.metrics import PHASE6_CALCULATION_VERSION
from app.evals.runner import (
    LOCAL_HASH_EMBEDDING_MODEL,
    SUPPORTED_EVALUATION_TYPES,
    EvaluationRunnerConfigError,
    EvaluationRunnerSourceMismatchError,
    EvaluationRunnerValidationError,
    evaluate_existing_run,
)
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    Feedback,
    FeedbackAnalysis,
    Project,
)
from app.models.evaluation import (
    EvaluationCase,
    EvaluationCaseErrorType,
    EvaluationCaseSource,
    EvaluationMetric,
    EvaluationRun,
    EvaluationRunStatus,
)

# ---- Fixture ----------------------------------------------------------------


@pytest.fixture
def db():
    """Provide a Session bound to a private in-memory SQLite.

    This file's runner tests are validated against SQLite only. The
    shared migration database that ``scripts/verify_postgres.py``
    drives must NOT be touched by this fixture — ``create_all`` /
    ``drop_all`` here would either build a schema that drifts from
    the Alembic migrations (e.g. missing
    ``ix_evaluation_cases_stage_case_id``) or wipe rows / the
    ``alembic_version`` entry that the subsequent ``alembic
    downgrade base`` step in the shared verification script relies
    on. Tests in this file therefore ignore ``TEST_DATABASE_URL``
    and always use the in-memory engine below. Real PG runner
    validation is left to a dedicated migration-fresh database that
    Codex will run independently.
    """
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
class _ClassificationFixture:
    project: Project
    feedbacks: list[Feedback]
    run: AnalysisRun


def _seed_classification(
    db: Session,
    *,
    n_feedback: int = 3,
    missing_prediction_external_id: str | None = None,
    feedback_contents: tuple[str, ...] = ("alpha text", "beta text", "gamma text"),
    type_plan: tuple[str, ...] = ("bug", "feature_request", "ux_issue"),
    severity_plan: tuple[int, ...] = (3, 4, 2),
    unknown_token_calls: int = 0,
    unknown_latency: bool = False,
) -> _ClassificationFixture:
    """Create Project + Feedbacks + FeedbackAnalyses + completed AnalysisRun.

    The AnalysisRun's counters satisfy the source table's CHECK
    constraint: ``success_count + failed_count <= processed_count``.
    When ``missing_prediction_external_id`` is set, the corresponding
    case has no ``FeedbackAnalysis`` row, so it counts as "not
    processed" (``processed_count`` excludes it, ``failed_count`` stays
    0). The runner still surfaces it as ``no_prediction`` in the
    per-case rows.
    """
    project = Project(name="Phase6", product_name="test")
    db.add(project)
    db.flush()

    processed = n_feedback
    success = n_feedback
    if missing_prediction_external_id is not None:
        processed = n_feedback - 1
        success = n_feedback - 1

    run = AnalysisRun(
        project_id=project.id,
        run_type="classification",
        model="test-model",
        prompt_version="classification-v1",
        status="completed",
        input_count=n_feedback,
        processed_count=processed,
        success_count=success,
        failed_count=0,
        input_tokens=120,
        output_tokens=60,
        unknown_token_calls=unknown_token_calls,
        # ``latency_ms`` and ``attempt_count`` are NOT NULL on the source
        # table with ``>= 0`` CHECK, so we cannot model "unknown" with
        # NULL. The runner treats ``attempt_count == 0`` OR
        # ``latency_ms == 0`` as NOT_MEASURED and records NULL on the
        # eval run — that is the contract we are asserting.
        latency_ms=0 if unknown_latency else 1234,
        attempt_count=0 if unknown_latency else 1,
        retry_count=0,
    )
    db.add(run)
    db.flush()

    feedbacks: list[Feedback] = []
    for i in range(n_feedback):
        ext_id = f"F{i + 1:03d}"
        fb = Feedback(
            project_id=project.id,
            external_id=ext_id,
            content=feedback_contents[i],
        )
        db.add(fb)
        db.flush()
        feedbacks.append(fb)

        if missing_prediction_external_id and ext_id == missing_prediction_external_id:
            # No FeedbackAnalysis for this case_id → dataset case will have
            # no_prediction error_type.
            continue

        db.add(
            FeedbackAnalysis(
                feedback_id=fb.id,
                run_id=run.id,
                feedback_type=type_plan[i % len(type_plan)],
                module=f"module_{i}",
                intent=f"intent_{i}",
                pain_point=f"pain_{i}",
                sentiment="negative",
                severity=severity_plan[i % len(severity_plan)],
                confidence=0.9,
            )
        )
    db.commit()
    return _ClassificationFixture(project=project, feedbacks=feedbacks, run=run)


def _make_classification_dataset(
    *, feedbacks: list[Feedback], labels: dict[str, dict[str, object]]
) -> DatasetProvenance:
    """Build a DatasetProvenance whose case text matches Feedback.content byte-for-byte."""
    cases: list[DatasetCase] = []
    for fb in feedbacks:
        case_labels = labels.get(fb.external_id, {})
        cases.append(
            DatasetCase(
                case_id=fb.external_id,
                feedback_text=fb.content,
                feedback_type_label=case_labels.get("feedback_type"),
                severity_label=case_labels.get("severity"),
                cluster_label=case_labels.get("cluster"),
            )
        )
    return DatasetProvenance(
        name="test_classification_dataset",
        version=build_dataset_version(cases),
        content_hash=hash_dataset_cases(cases),
        provenance="synthetic",
        split=DATASET_SPLIT_FULL,
        label_status="test_synthetic_reference_labels_unreviewed",
        cases=tuple(cases),
        cases_by_id={c.case_id: c for c in cases},
        source_paths=(),
    )


# ---- Classification happy path ----------------------------------------------


def test_classification_create_and_complete(db: Session):
    fx = _seed_classification(
        db,
        type_plan=("bug", "feature_request", "ux_issue"),
        severity_plan=(3, 4, 2),
    )
    labels = {
        "F001": {"feedback_type": "bug", "severity": 3},
        "F002": {"feedback_type": "feature_request", "severity": 4},
        "F003": {"feedback_type": "ux_issue", "severity": 2},
    }
    dataset = _make_classification_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED.value
    assert run.input_count == 3
    assert run.processed_count == 3
    assert run.success_count == 3
    assert run.failed_count == 0
    assert run.calculation_version == PHASE6_CALCULATION_VERSION
    assert run.calculation_version.startswith("phase6-")
    assert run.input_tokens == 120
    assert run.output_tokens == 60
    assert run.latency_ms == 1234
    assert run.unknown_token_calls == 0
    assert run.model == "test-model"
    assert run.prompt_version == "classification-v1"
    assert run.stage == "classification"
    assert run.evaluation_type == "classification_metrics"
    # EvaluationMetric rows: feedback_type_accuracy + macro_f1 +
    # severity_exact + mae + pipeline_success_rate +
    # pipeline_failure_stage_count = 6
    metrics = db.scalars(select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)).all()
    metric_names = sorted(m.metric_name for m in metrics)
    assert metric_names == [
        "feedback_type_accuracy",
        "feedback_type_macro_f1",
        "pipeline_failure_stage_count",
        "pipeline_success_rate",
        "severity_exact_match",
        "severity_mae",
    ]
    for m in metrics:
        assert m.run_id == run.id
        assert m.project_id == fx.project.id
        assert m.metric_kind == "deterministic"
    accuracy = next(m for m in metrics if m.metric_name == "feedback_type_accuracy")
    assert accuracy.sample_count == 3
    assert accuracy.value == pytest.approx(1.0)
    mae = next(m for m in metrics if m.metric_name == "severity_mae")
    assert mae.sample_count == 3
    assert mae.value == pytest.approx(0.0)
    # Per-case rows
    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    assert len(cases) == 3
    for c in cases:
        assert c.project_id == fx.project.id
        assert c.stage == "classification"
        assert c.passed is True
        assert c.error_type is None
        # Raw text NOT persisted by default.
        assert c.input is None
        assert c.expected is None
        assert c.actual is None
        # Hashes ARE always persisted.
        assert c.input_hash
        assert c.expected_hash
        assert c.actual_hash
    # Mutually consistent counts
    assert run.success_count + run.failed_count == run.processed_count
    assert run.processed_count <= run.input_count


def test_classification_partial_fail_when_prediction_missing(db: Session):
    fx = _seed_classification(
        db,
        missing_prediction_external_id="F002",
        type_plan=("bug", "feature_request"),
        severity_plan=(3, 4),
        feedback_contents=("alpha text", "beta text", "gamma text"),
    )
    labels = {
        "F001": {"feedback_type": "bug", "severity": 3},
        "F002": {"feedback_type": "feature_request", "severity": 4},
        "F003": {"feedback_type": "other", "severity": 5},
    }
    dataset = _make_classification_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    assert run.status == EvaluationRunStatus.PARTIAL_FAILED.value
    assert run.processed_count == 3
    assert run.success_count == 1
    assert run.failed_count == 2  # F002 no_prediction + F003 wrong labels
    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    f002 = next(c for c in cases if c.case_id == "F002")
    assert f002.error_type == EvaluationCaseErrorType.NO_PREDICTION.value
    assert f002.passed is False
    f003 = next(c for c in cases if c.case_id == "F003")
    assert f003.passed is False
    assert f003.error_type == EvaluationCaseErrorType.OTHER.value


def test_classification_wrong_run_type_rejected(db: Session):
    fx = _seed_classification(db)
    # Switch the source run to clustering so its run_type does not match
    # the requested classification_metrics evaluation_type.
    db.execute(
        AnalysisRun.__table__.update()
        .where(AnalysisRun.id == fx.run.id)
        .values(run_type="clustering")
    )
    db.commit()
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks, labels={"F001": {"feedback_type": "bug"}}
    )
    with pytest.raises(EvaluationRunnerSourceMismatchError) as exc_info:
        evaluate_existing_run(
            db,
            project_id=fx.project.id,
            source_run_id=fx.run.id,
            evaluation_type="classification_metrics",
            dataset=dataset,
        )
    assert any("run_type" in e for e in exc_info.value.errors)
    # No EvaluationRun row was written.
    assert db.scalar(select(EvaluationRun).where(EvaluationRun.project_id == fx.project.id)) is None


def test_classification_cross_project_rejected(db: Session):
    fx = _seed_classification(db)
    # Build a second project; the source run's project is the first one.
    other = Project(name="other", product_name="test")
    db.add(other)
    db.commit()
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks, labels={"F001": {"feedback_type": "bug"}}
    )
    with pytest.raises(EvaluationRunnerSourceMismatchError) as exc_info:
        evaluate_existing_run(
            db,
            project_id=other.id,
            source_run_id=fx.run.id,
            evaluation_type="classification_metrics",
            dataset=dataset,
        )
    assert any("cross-project" in e for e in exc_info.value.errors)
    assert db.scalar(select(EvaluationRun).where(EvaluationRun.project_id == other.id)) is None


def test_classification_non_terminal_run_rejected(db: Session):
    fx = _seed_classification(db)
    db.execute(
        AnalysisRun.__table__.update().where(AnalysisRun.id == fx.run.id).values(status="running")
    )
    db.commit()
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks, labels={"F001": {"feedback_type": "bug"}}
    )
    with pytest.raises(EvaluationRunnerSourceMismatchError) as exc_info:
        evaluate_existing_run(
            db,
            project_id=fx.project.id,
            source_run_id=fx.run.id,
            evaluation_type="classification_metrics",
            dataset=dataset,
        )
    assert any("not terminal" in e for e in exc_info.value.errors)


def test_classification_dataset_mismatch_rejected(db: Session):
    """Drift between dataset.feedback_text and Feedback.content is rejected."""
    fx = _seed_classification(db)
    # Build a dataset that claims F001 has different text than the stored Feedback.
    bad_cases = [
        DatasetCase(
            case_id="F001",
            feedback_text="different text that the demo CSV never had",
            feedback_type_label="bug",
            severity_label=3,
        )
    ]
    dataset = DatasetProvenance(
        name="drift",
        version=build_dataset_version(bad_cases),
        content_hash=hash_dataset_cases(bad_cases),
        provenance="synthetic",
        split=DATASET_SPLIT_FULL,
        label_status="test",
        cases=tuple(bad_cases),
        cases_by_id={c.case_id: c for c in bad_cases},
        source_paths=(),
    )
    with pytest.raises(EvaluationRunnerValidationError) as exc_info:
        evaluate_existing_run(
            db,
            project_id=fx.project.id,
            source_run_id=fx.run.id,
            evaluation_type="classification_metrics",
            dataset=dataset,
        )
    assert any("content hash mismatch" in e for e in exc_info.value.errors)
    assert db.scalar(select(EvaluationRun).where(EvaluationRun.project_id == fx.project.id)) is None


def test_classification_raw_text_opt_in(db: Session):
    fx = _seed_classification(
        db,
        type_plan=("bug",),
        severity_plan=(3,),
        feedback_contents=("alpha text",),
        n_feedback=1,
    )
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks, labels={"F001": {"feedback_type": "bug", "severity": 3}}
    )
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
        store_raw=True,
    )
    assert run.status == EvaluationRunStatus.COMPLETED.value
    assert "store_raw=True" in (run.notes or "")
    case = db.scalar(
        select(EvaluationCase).where(
            EvaluationCase.run_id == run.id, EvaluationCase.case_id == "F001"
        )
    )
    assert case.input == "alpha text"
    assert case.input_hash  # hash is still recorded
    # expected/actual stay None unless we also wrote them
    assert case.expected is None or case.expected.startswith("feedback_type=")


def test_classification_unknown_token_calls_propagates(db: Session):
    fx = _seed_classification(
        db,
        unknown_token_calls=2,
        unknown_latency=True,
        feedback_contents=("alpha text",),
        type_plan=("bug",),
        severity_plan=(3,),
        n_feedback=1,
    )
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks, labels={"F001": {"feedback_type": "bug", "severity": 3}}
    )
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    # When the source flags unreliable calls via ``unknown_token_calls``
    # the eval run records NULL for input/output tokens (per the brief:
    # "input/output tokens 如 unknown_token_calls>0 也应 NULL"). The
    # unknown-latency case uses ``attempt_count=0`` so the eval run's
    # ``latency_ms`` is also NULL. ``unknown_token_calls`` itself is
    # propagated as-is so downstream regressions can detect it.
    assert run.input_tokens is None
    assert run.output_tokens is None
    assert run.latency_ms is None
    assert run.unknown_token_calls == 2


def test_unknown_evaluation_type_rejected(db: Session):
    fx = _seed_classification(db)
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks, labels={"F001": {"feedback_type": "bug"}}
    )
    with pytest.raises(EvaluationRunnerConfigError, match="unsupported evaluation_type"):
        evaluate_existing_run(
            db,
            project_id=fx.project.id,
            source_run_id=fx.run.id,
            evaluation_type="pipeline_health",
            dataset=dataset,
        )


def test_runner_internal_exception_marks_run_failed(db: Session, monkeypatch):
    """If the metric layer blows up, the runner records a single failed row."""
    fx = _seed_classification(db)
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks,
        labels={
            "F001": {"feedback_type": "bug", "severity": 3},
            "F002": {"feedback_type": "feature_request", "severity": 4},
            "F003": {"feedback_type": "ux_issue", "severity": 2},
        },
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated metric explosion")

    monkeypatch.setattr("app.evals.runner.compute_feedback_type_metrics", _boom)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED.value
    assert run.error_message and "simulated metric explosion" in run.error_message
    # The CHECK constraint ``success_count + failed_count <= processed_count``
    # must hold for the failed row too. Nothing was actually processed
    # so the per-case counters all stay 0; the plan size lives in
    # ``input_count``.
    assert run.processed_count == 0
    assert run.success_count == 0
    assert run.failed_count == 0
    assert run.input_count == 3
    assert run.success_count + run.failed_count <= run.processed_count
    # Exactly ONE EvaluationRun row was written for this project.
    rows = db.scalars(select(EvaluationRun).where(EvaluationRun.project_id == fx.project.id)).all()
    assert len(rows) == 1
    # No metric / case rows leaked.
    assert db.scalar(select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)) is None
    assert db.scalar(select(EvaluationCase).where(EvaluationCase.run_id == run.id)) is None


def test_failed_run_satisfies_count_check_constraint(db: Session, monkeypatch):
    """The failed-row counts must satisfy ``ck_evaluation_runs_non_negative_counts``.

    Re-asserts the invariant with a large dataset so a regression in
    the failed-row defaults would surface here. The DB itself enforces
    the CHECK; the test exists so a refactor of the runner's failed
    defaults can never silently violate the constraint.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("deliberate failure")

    feedback_contents = tuple(f"text {i}" for i in range(10))
    fx = _seed_classification(
        db,
        n_feedback=10,
        feedback_contents=feedback_contents,
        type_plan=tuple("bug" for _ in range(10)),
        severity_plan=tuple(3 for _ in range(10)),
    )
    labels = {f"F{i + 1:03d}": {"feedback_type": "bug", "severity": 3} for i in range(10)}
    dataset = _make_classification_dataset(feedbacks=fx.feedbacks, labels=labels)
    monkeypatch.setattr("app.evals.runner.compute_feedback_type_metrics", _boom)

    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED.value
    assert run.input_count == 10
    assert run.processed_count == 0
    assert run.success_count == 0
    assert run.failed_count == 0
    # The literal constraint body — a regression in the default
    # counters MUST trip this assertion rather than the DB CHECK.
    assert run.success_count + run.failed_count <= run.processed_count
    assert run.processed_count <= run.input_count
    assert 0 <= run.success_count
    assert 0 <= run.failed_count


def test_failed_run_error_message_strips_sql_and_parameters(db: Session, monkeypatch):
    """``error_message`` must never echo SQL, parameters, or credentials.

    The brief: "异常 error_message 当前包含 IntegrityError SQL/parameters
    原文，可能泄露用户输入；只存安全摘要，不含 SQL、参数、密钥". The runner
    keeps the exception type name and (when safe) a short reason; SQL
    fragments, parameter values and credential-like tokens are
    stripped before the row is committed.
    """

    class _SqlLeakError(Exception):
        pass

    def _boom(*args, **kwargs):
        raise _SqlLeakError(
            "(IntegrityError) CHECK constraint failed: ck_foo\n"
            "[SQL: INSERT INTO evaluation_runs (id, project_id) "
            "VALUES (?, ?)]\n"
            "[parameters: ('SECRET-UUID-1', 'SECRET-UUID-2')]\n"
            "Authorization: Bearer sk-abc12345xyz9876"
        )

    fx = _seed_classification(db, n_feedback=2)
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks,
        labels={
            "F001": {"feedback_type": "bug", "severity": 3},
            "F002": {"feedback_type": "feature_request", "severity": 4},
        },
    )
    monkeypatch.setattr("app.evals.runner.compute_feedback_type_metrics", _boom)

    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED.value
    assert run.error_message
    # SQL / parameters / credential-like content MUST be gone. The
    # checks below are deliberately specific to the leaked payload so
    # they don't trip on a coincidental substring of the type name
    # ``_SqlLeakError`` we are allowed to keep.
    assert "[SQL" not in run.error_message
    assert "INSERT INTO" not in run.error_message
    assert "[parameters" not in run.error_message
    assert "VALUES (" not in run.error_message
    assert "SECRET-UUID-1" not in run.error_message
    assert "SECRET-UUID-2" not in run.error_message
    assert "sk-abc12345xyz9876" not in run.error_message
    assert "Bearer " not in run.error_message
    # The exception class name is the only identifier we keep.
    assert "_SqlLeakError" in run.error_message
    # The runner marks the row as a crash so an audit can find it.
    assert "runner crashed" in run.error_message


def test_failed_run_error_message_redacts_credentials(db: Session, monkeypatch):
    """Credential-like tokens are redacted even when no SQL marker fires."""

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "transport failed: Authorization: Bearer sk-abc12345xyz9876, "
            "fallback api_key=DEADBEEF sk-1234abcd5678"
        )

    fx = _seed_classification(db, n_feedback=2)
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks,
        labels={
            "F001": {"feedback_type": "bug", "severity": 3},
            "F002": {"feedback_type": "feature_request", "severity": 4},
        },
    )
    monkeypatch.setattr("app.evals.runner.compute_feedback_type_metrics", _boom)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED.value
    assert run.error_message
    # No raw credential.
    assert "sk-abc12345xyz9876" not in run.error_message
    assert "sk-1234abcd5678" not in run.error_message
    assert "api_key=DEADBEEF" not in run.error_message
    # The human-readable reason is still kept (the redactor only
    # replaces credential-like substrings).
    assert "transport failed" in run.error_message
    assert "RuntimeError" in run.error_message


def test_source_run_fixture_satisfies_non_negative_counts(db: Session):
    """Both seed helpers must produce a source AnalysisRun that passes
    ``ck_analysis_runs_non_negative_counts``.
    """
    fx = _seed_classification(db, n_feedback=3)
    db.refresh(fx.run)
    assert fx.run.success_count + fx.run.failed_count <= fx.run.processed_count
    assert fx.run.processed_count <= fx.run.input_count
    assert 0 <= fx.run.success_count
    assert 0 <= fx.run.failed_count

    # With a missing-prediction case the seed sets ``processed_count``
    # one below ``input_count`` and keeps ``failed_count`` at 0.
    fx_missing = _seed_classification(db, n_feedback=3, missing_prediction_external_id="F002")
    db.refresh(fx_missing.run)
    assert (
        fx_missing.run.success_count + fx_missing.run.failed_count <= fx_missing.run.processed_count
    )
    assert fx_missing.run.processed_count <= fx_missing.run.input_count
    assert fx_missing.run.failed_count == 0

    # Cluster seed: every member is processed + succeeded.
    fx_cluster = _seed_cluster_run(db, n_feedback=4)
    db.refresh(fx_cluster.run)
    assert (
        fx_cluster.run.success_count + fx_cluster.run.failed_count <= fx_cluster.run.processed_count
    )
    assert fx_cluster.run.processed_count <= fx_cluster.run.input_count


def test_supported_evaluation_types_constant_lists_two():
    assert set(SUPPORTED_EVALUATION_TYPES) == {
        "classification_metrics",
        "cluster_metrics",
    }
    assert LOCAL_HASH_EMBEDDING_MODEL == "local-hash-v1"


# ---- Pipeline success / failure metrics --------------------------------------


def _metric_rows_by_name(db: Session, *, run_id: object) -> dict[str, EvaluationMetric]:
    return {
        m.metric_name: m
        for m in db.scalars(select(EvaluationMetric).where(EvaluationMetric.run_id == run_id)).all()
    }


def test_pipeline_metrics_full_completion_even_when_label_accuracy_low(db: Session):
    """Source 12/12 completed → pipeline_success_rate=1.0 even if labels are wrong.

    Codex's real baseline model processed 12/12 cases successfully but
    labelled roughly two-thirds wrong. The pipeline metric MUST stay
    at 1.0 in that scenario so the dashboard does not confuse a label
    accuracy regression with a source-pipeline execution regression.
    """
    feedback_contents = tuple(f"text {i}" for i in range(12))
    fx = _seed_classification(
        db,
        n_feedback=12,
        feedback_contents=feedback_contents,
        type_plan=tuple("bug" for _ in range(12)),
        severity_plan=tuple(3 for _ in range(12)),
    )
    # Every dataset label is the OPPOSITE of what the source predicted:
    # label accuracy collapses to 0 while pipeline execution is perfect.
    labels = {
        f"F{i + 1:03d}": {"feedback_type": "feature_request", "severity": 5} for i in range(12)
    }
    dataset = _make_classification_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    metrics_by_name = _metric_rows_by_name(db, run_id=run.id)
    assert "pipeline_success_rate" in metrics_by_name
    psm = metrics_by_name["pipeline_success_rate"]
    assert psm.stage == "pipeline"
    assert psm.metric_kind == "deterministic"
    assert psm.sample_count == 12
    assert psm.value == pytest.approx(1.0)
    details = dict(psm.details)
    assert details["source_run_id"] == str(fx.run.id)
    assert details["source_status"] == "completed"
    assert details["success_count"] == 12
    assert details["failed_count"] == 0
    assert details["processed_count"] == 12
    assert details["input_count"] == 12
    assert details["stage"] == "classification"
    # The brief mandates this exact provenance string so a regression
    # / dashboard panel can confirm the metric is intentionally
    # decoupled from per-label accuracy.
    assert details["definition"] == "source_pipeline_execution_not_label_accuracy"
    # Sanity: confirm the label-accuracy IS low — that's the whole
    # point of decoupling the two channels.
    assert metrics_by_name["feedback_type_accuracy"].value == pytest.approx(0.0)
    # The matching failure-count row must show 0 for this run.
    pfscm = metrics_by_name["pipeline_failure_stage_count"]
    assert pfscm.stage == "pipeline"
    assert pfscm.sample_count == 12
    assert pfscm.value == pytest.approx(0.0)
    assert dict(pfscm.details)["failure_stage_label"] == "classification"


def test_pipeline_metrics_reflect_source_partial_failed(db: Session):
    """A ``partial_failed`` source run yields pipeline_success_rate<1.0.

    The metric mirrors the source counters verbatim — a ``partial_failed``
    / ``failed`` source run must not be sanitised into a 100% rate just
    because the eval-side per-case comparison happens to match the GT
    labels for the cases that did get processed.
    """
    fx = _seed_classification(
        db,
        n_feedback=12,
        feedback_contents=tuple(f"text {i}" for i in range(12)),
        type_plan=tuple("bug" for _ in range(12)),
        severity_plan=tuple(3 for _ in range(12)),
    )
    # The source claims it only managed to process 10/12 and 2 of those
    # attempts failed. The dataset labels still match the predictions
    # so eval-side accuracy is perfect — only the pipeline rate should
    # surface the execution drop.
    db.execute(
        AnalysisRun.__table__.update()
        .where(AnalysisRun.id == fx.run.id)
        .values(
            status="partial_failed",
            input_count=12,
            processed_count=10,
            success_count=8,
            failed_count=2,
        )
    )
    db.commit()
    db.refresh(fx.run)
    labels = {f"F{i + 1:03d}": {"feedback_type": "bug", "severity": 3} for i in range(12)}
    dataset = _make_classification_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    metrics_by_name = _metric_rows_by_name(db, run_id=run.id)
    psm = metrics_by_name["pipeline_success_rate"]
    assert psm.stage == "pipeline"
    assert psm.sample_count == 12
    assert psm.value == pytest.approx(8.0 / 12.0)
    assert dict(psm.details)["source_status"] == "partial_failed"
    assert dict(psm.details)["source_run_id"] == str(fx.run.id)
    assert dict(psm.details)["failed_count"] == 2
    assert dict(psm.details)["success_count"] == 8
    pfscm = metrics_by_name["pipeline_failure_stage_count"]
    assert pfscm.stage == "pipeline"
    assert pfscm.sample_count == 12
    assert pfscm.value == pytest.approx(2.0)
    assert dict(pfscm.details)["failure_stage_label"] == "classification"
    # The eval run's own per-case counters are unrelated to the
    # pipeline metric — confirm they still follow their own rules.
    assert run.input_count == 12


def test_pipeline_metrics_return_null_when_source_input_count_zero(db: Session):
    """Source had zero inputs → pipeline metrics report NOT_MEASURED.

    The ORM CHECK ``(sample_count > 0) OR (value IS NULL)`` must be
    honoured: ``sample_count=0`` is paired with ``value=None`` and the
    row documents why in ``details``.
    """
    fx = _seed_classification(
        db,
        n_feedback=1,
        feedback_contents=("alpha text",),
        type_plan=("bug",),
        severity_plan=(3,),
    )
    db.execute(
        AnalysisRun.__table__.update()
        .where(AnalysisRun.id == fx.run.id)
        .values(
            status="completed",
            input_count=0,
            processed_count=0,
            success_count=0,
            failed_count=0,
        )
    )
    db.commit()
    db.refresh(fx.run)
    labels = {"F001": {"feedback_type": "bug", "severity": 3}}
    dataset = _make_classification_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    metrics_by_name = _metric_rows_by_name(db, run_id=run.id)
    psm = metrics_by_name["pipeline_success_rate"]
    assert psm.sample_count == 0
    assert psm.value is None
    assert psm.stage == "pipeline"
    assert dict(psm.details)["input_count"] == 0
    assert dict(psm.details)["not_measured_reason"] == "source_run_input_count_zero"
    pfscm = metrics_by_name["pipeline_failure_stage_count"]
    assert pfscm.sample_count == 0
    assert pfscm.value is None
    assert pfscm.stage == "pipeline"
    assert dict(pfscm.details)["input_count"] == 0
    assert dict(pfscm.details)["not_measured_reason"] == "source_run_input_count_zero"


def test_pipeline_metrics_attach_for_cluster_path(db: Session):
    """Cluster path also emits pipeline metrics with the clustering label."""
    fx = _seed_cluster_run(db)
    labels = {fb.external_id: f"topic_{i}" for i, fb in enumerate(fx.feedbacks)}
    dataset = _make_cluster_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="cluster_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    metrics_by_name = _metric_rows_by_name(db, run_id=run.id)
    assert "pipeline_success_rate" in metrics_by_name
    assert "pipeline_failure_stage_count" in metrics_by_name
    psm = metrics_by_name["pipeline_success_rate"]
    assert psm.stage == "pipeline"
    assert psm.sample_count == len(fx.feedbacks)
    assert psm.value == pytest.approx(1.0)
    assert dict(psm.details)["stage"] == "clustering"
    assert dict(psm.details)["definition"] == "source_pipeline_execution_not_label_accuracy"
    pfscm = metrics_by_name["pipeline_failure_stage_count"]
    assert pfscm.stage == "pipeline"
    assert pfscm.sample_count == len(fx.feedbacks)
    assert pfscm.value == pytest.approx(0.0)
    assert dict(pfscm.details)["failure_stage_label"] == "clustering"


# ---- Cluster path ------------------------------------------------------------


@dataclass(frozen=True)
class _ClusterFixture:
    project: Project
    feedbacks: list[Feedback]
    run: AnalysisRun
    cluster_id: UUID
    duplicate_cluster_id: UUID | None


def _seed_cluster_run(
    db: Session,
    *,
    n_feedback: int = 4,
    duplicate_external_id: str | None = None,
    embedding_model: str = LOCAL_HASH_EMBEDDING_MODEL,
    feedback_contents: tuple[str, ...] = ("alpha text", "beta text", "gamma text", "delta text"),
) -> _ClusterFixture:
    project = Project(name="Phase6-cluster", product_name="test")
    db.add(project)
    db.flush()

    # Classification run (sibling) that the cluster run references via
    # input_feedback_external_ids.
    class_run = AnalysisRun(
        project_id=project.id,
        run_type="classification",
        model="test-model",
        prompt_version="classification-v1",
        status="completed",
        input_count=n_feedback,
        processed_count=n_feedback,
        success_count=n_feedback,
    )
    db.add(class_run)
    db.flush()

    feedbacks: list[Feedback] = []
    for i in range(n_feedback):
        fb = Feedback(
            project_id=project.id,
            external_id=f"F{i + 1:03d}",
            content=feedback_contents[i],
        )
        db.add(fb)
        db.flush()
        feedbacks.append(fb)
        db.add(
            FeedbackAnalysis(
                feedback_id=fb.id,
                run_id=class_run.id,
                feedback_type="feature_request",
                module="m",
                intent="i",
                pain_point="p",
                sentiment="negative",
                severity=3,
                confidence=0.9,
            )
        )

    run = AnalysisRun(
        project_id=project.id,
        run_type="clustering",
        model="test-model",
        prompt_version="cluster-review-v4",
        status="completed",
        input_count=n_feedback,
        processed_count=n_feedback,
        success_count=n_feedback,
        input_tokens=200,
        output_tokens=80,
        unknown_token_calls=0,
        latency_ms=5000,
        # ``attempt_count`` MUST be > 0 for the eval run to carry the
        # source's ``latency_ms`` through. The runner treats
        # ``attempt_count == 0`` as NOT_MEASURED and records NULL on
        # the eval latency column.
        attempt_count=1,
        cluster_config={
            "embedding_model": embedding_model,
            "classification_run_id": str(class_run.id),
        },
    )
    db.add(run)
    db.flush()

    cluster = Cluster(
        project_id=project.id,
        run_id=run.id,
        embedding_model=embedding_model,
        distance_threshold=0.5,
        linkage="average",
    )
    db.add(cluster)
    db.flush()

    duplicate_cluster_id: UUID | None = None
    for i, fb in enumerate(feedbacks):
        db.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=db.scalar(
                    select(FeedbackAnalysis.id).where(FeedbackAnalysis.feedback_id == fb.id)
                ),
                similarity_score=0.9 - 0.05 * i,
            )
        )
        if duplicate_external_id and fb.external_id == duplicate_external_id:
            # Add the same feedback_analysis to a second cluster to
            # exercise the duplicate-diagnosis path.
            duplicate_cluster = Cluster(
                project_id=project.id,
                run_id=run.id,
                embedding_model=embedding_model,
                distance_threshold=0.5,
                linkage="average",
            )
            db.add(duplicate_cluster)
            db.flush()
            duplicate_cluster_id = duplicate_cluster.id
            analysis_id = db.scalar(
                select(FeedbackAnalysis.id).where(FeedbackAnalysis.feedback_id == fb.id)
            )
            db.add(
                ClusterMember(
                    cluster_id=duplicate_cluster.id,
                    feedback_analysis_id=analysis_id,
                    similarity_score=0.4,
                )
            )
    db.commit()
    return _ClusterFixture(
        project=project,
        feedbacks=feedbacks,
        run=run,
        cluster_id=cluster.id,
        duplicate_cluster_id=duplicate_cluster_id,
    )


def _make_cluster_dataset(
    *, feedbacks: list[Feedback], labels: dict[str, str]
) -> DatasetProvenance:
    cases = [
        DatasetCase(
            case_id=fb.external_id,
            feedback_text=fb.content,
            cluster_label=labels.get(fb.external_id),
        )
        for fb in feedbacks
    ]
    return DatasetProvenance(
        name="test_cluster_dataset",
        version=build_dataset_version(cases),
        content_hash=hash_dataset_cases(cases),
        provenance="synthetic",
        split=DATASET_SPLIT_FULL,
        label_status="test_cluster_labels_unreviewed",
        cases=tuple(cases),
        cases_by_id={c.case_id: c for c in cases},
        source_paths=(),
    )


def test_cluster_create_and_complete(db: Session):
    fx = _seed_cluster_run(db)
    # All feedbacks are in ONE predicted cluster (the seed helper puts
    # them all under ``cluster.id``). The GT labels must mirror that:
    # a single "topic_0" cluster for every case. Mixing distinct
    # labels per feedback would force the runner into a 4-GT vs
    # 1-predicted configuration and drag cluster_purity_micro down
    # to 1/4 = 0.25, which would mask a real linkage bug.
    labels = {fb.external_id: "topic_0" for fb in fx.feedbacks}
    dataset = _make_cluster_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="cluster_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED.value
    assert run.evaluation_type == "cluster_metrics"
    assert run.stage == "clustering"
    # All cases labelled the same way, all predicted in the same cluster:
    # purity should be 1.0, coverage 1.0, fragmentation 1.0.
    metrics = {
        m.metric_name: m
        for m in db.scalars(select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)).all()
    }
    assert "cluster_count" in metrics
    assert "cluster_purity_micro" in metrics
    assert "cluster_coverage" in metrics
    assert "cluster_fragmentation_mean" in metrics
    assert "cluster_over_merge_ratio" in metrics
    assert metrics["cluster_count"].sample_count >= 1
    assert metrics["cluster_purity_micro"].value == pytest.approx(1.0)
    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    assert len(cases) == 4
    for c in cases:
        # Default policy: ``expected`` / ``actual`` stay NULL on the row
        # so no sensitive plaintext is persisted. The cluster runner
        # still records SHA-256 of those values plus a pass/fail flag,
        # which is what the brief asks the audit trail to carry.
        assert c.expected is None
        assert c.actual is None
        assert c.expected_hash  # SHA-256 of "gt_cluster=topic_0"
        assert c.actual_hash  # SHA-256 of "predicted_cluster=<uuid>"
        assert c.passed is True
    # Token / latency / unknown_token_calls propagate. The cluster
    # seed reports ``unknown_token_calls == 0`` so the input/output
    # totals stay numeric; ``latency_ms == 5000`` survives because
    # the seed also sets ``attempt_count = 1`` (the runner treats
    # ``attempt_count == 0`` as NOT_MEASURED and would NULL the
    # eval latency otherwise).
    assert run.input_tokens == 200
    assert run.output_tokens == 80
    assert run.latency_ms == 5000
    assert run.unknown_token_calls == 0
    # Embedding model is captured on the run
    assert run.model == LOCAL_HASH_EMBEDDING_MODEL
    assert "NOT a real semantic embedding" in (run.notes or "")
    assert run.pipeline_version == "test-model"  # source run.model


def test_cluster_duplicate_member_marked(db: Session):
    fx = _seed_cluster_run(db, duplicate_external_id="F002")
    labels = {fb.external_id: f"topic_{i}" for i, fb in enumerate(fx.feedbacks)}
    dataset = _make_cluster_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="cluster_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    # At least one case (F002) is in 2 clusters → not a 1-to-1 mapping.
    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    f002 = next(c for c in cases if c.case_id == "F002")
    assert f002.passed is False
    assert f002.error_type == EvaluationCaseErrorType.OTHER.value
    assert f002.notes and "multiple clusters" in f002.notes
    assert "F002" in (run.notes or "")
    assert run.status == EvaluationRunStatus.PARTIAL_FAILED.value


def test_cluster_local_hash_warning_present(db: Session):
    fx = _seed_cluster_run(db, embedding_model=LOCAL_HASH_EMBEDDING_MODEL)
    labels = {fb.external_id: f"topic_{i}" for i, fb in enumerate(fx.feedbacks)}
    dataset = _make_cluster_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="cluster_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    notes = run.notes or ""
    assert "source_embedding_model=local-hash-v1" in notes
    assert "NOT a real semantic embedding" in notes
    assert "source_cluster_config=" in notes


def test_cluster_remote_embedding_no_local_hash_warning(db: Session):
    fx = _seed_cluster_run(db, embedding_model="text-embedding-3-small")
    labels = {fb.external_id: f"topic_{i}" for i, fb in enumerate(fx.feedbacks)}
    dataset = _make_cluster_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="cluster_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    notes = run.notes or ""
    assert "NOT a real semantic embedding" not in notes
    assert "source_embedding_model=text-embedding-3-small" in notes


def test_cluster_dataset_without_cluster_labels_rejected(db: Session):
    fx = _seed_cluster_run(db)
    # Build a dataset whose cases have no cluster_label → metric package
    # cannot score and the runner must refuse explicitly.
    bad_cases = [
        DatasetCase(case_id=fb.external_id, feedback_text=fb.content) for fb in fx.feedbacks
    ]
    dataset = DatasetProvenance(
        name="no_cluster",
        version=build_dataset_version(bad_cases),
        content_hash=hash_dataset_cases(bad_cases),
        provenance="synthetic",
        split=DATASET_SPLIT_FULL,
        label_status="test",
        cases=tuple(bad_cases),
        cases_by_id={c.case_id: c for c in bad_cases},
        source_paths=(),
    )
    with pytest.raises(EvaluationRunnerValidationError, match="cluster_label"):
        evaluate_existing_run(
            db,
            project_id=fx.project.id,
            source_run_id=fx.run.id,
            evaluation_type="cluster_metrics",
            dataset=dataset,
        )


def test_cluster_counts_and_metric_rows_remain_consistent(db: Session):
    fx = _seed_cluster_run(db, duplicate_external_id="F003")
    labels = {fb.external_id: f"topic_{i}" for i, fb in enumerate(fx.feedbacks)}
    dataset = _make_cluster_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="cluster_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
    ).all()
    case_rows = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    # Every metric + case row carries the right project_id.
    for row in metric_rows + case_rows:
        assert row.project_id == fx.project.id
    assert run.processed_count == len(case_rows)
    assert run.success_count + run.failed_count == run.processed_count
    assert run.processed_count <= run.input_count
    # metric_count > 0 (cluster metric set has 6 metrics)
    assert len(metric_rows) >= 6
    for m in metric_rows:
        # The CHECK constraint (sample_count > 0) OR (value IS NULL)
        # is enforced by the ORM; double-check we don't violate it.
        assert m.sample_count > 0 or m.value is None


def test_eval_sources_are_csv_load_by_default(db: Session):
    fx = _seed_classification(db)
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks,
        labels={
            "F001": {"feedback_type": "bug", "severity": 3},
            "F002": {"feedback_type": "feature_request", "severity": 4},
            "F003": {"feedback_type": "ux_issue", "severity": 2},
        },
    )
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    cases = db.scalars(select(EvaluationCase).where(EvaluationCase.run_id == run.id)).all()
    assert all(c.eval_source == EvaluationCaseSource.CSV_LOAD.value for c in cases)


def test_unknown_token_calls_propagated_when_source_run_unknown(db: Session):
    """A source run with all-unknown tokens still records NULL token columns.

    The source's ``input_tokens`` / ``output_tokens`` columns are NOT
    NULL on the old schema, so we cannot write NULL there to flag
    "unknown". The runner instead uses ``unknown_token_calls > 0`` as
    the unreliable-tokens trigger and writes NULL onto the eval run.
    """
    fx = _seed_cluster_run(
        db,
        n_feedback=3,
        feedback_contents=("alpha text", "beta text", "gamma text"),
    )
    db.execute(
        AnalysisRun.__table__.update()
        .where(AnalysisRun.id == fx.run.id)
        .values(unknown_token_calls=3)
    )
    db.commit()
    labels = {fb.external_id: f"topic_{i}" for i, fb in enumerate(fx.feedbacks)}
    dataset = _make_cluster_dataset(feedbacks=fx.feedbacks, labels=labels)
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="cluster_metrics",
        dataset=dataset,
    )
    db.refresh(run)
    assert run.input_tokens is None
    assert run.output_tokens is None
    assert run.unknown_token_calls == 3


# ---- Hygiene / cross-cutting ------------------------------------------------


def test_no_phase1_or_phase2_source_rows_are_mutated(db: Session):
    """The runner is strictly read-only against the source tables."""
    fx = _seed_classification(
        db,
        feedback_contents=("alpha text", "beta text", "gamma text"),
    )
    snap_before = {
        "feedbacks": [(f.id, f.external_id, f.content) for f in fx.feedbacks],
        "analyses": [
            (a.feedback_id, a.feedback_type, a.severity, a.intent, a.sentiment)
            for a in db.scalars(
                select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == fx.run.id)
            ).all()
        ],
        "run": (
            fx.run.input_count,
            fx.run.processed_count,
            fx.run.success_count,
            fx.run.failed_count,
            fx.run.status,
        ),
    }
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks,
        labels={
            "F001": {"feedback_type": "bug", "severity": 3},
            "F002": {"feedback_type": "feature_request", "severity": 4},
            "F003": {"feedback_type": "ux_issue", "severity": 2},
        },
    )
    evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
    )
    db.expire_all()
    snap_after = {
        "feedbacks": [
            (f.id, f.external_id, f.content)
            for f in db.scalars(select(Feedback).where(Feedback.project_id == fx.project.id)).all()
        ],
        "analyses": [
            (a.feedback_id, a.feedback_type, a.severity, a.intent, a.sentiment)
            for a in db.scalars(
                select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == fx.run.id)
            ).all()
        ],
        "run": (
            fx.run.input_count,
            fx.run.processed_count,
            fx.run.success_count,
            fx.run.failed_count,
            fx.run.status,
        ),
    }
    assert snap_before == snap_after


# Sanity: ensure the test runner function signature is callable with the
# documented keyword-only arguments (intent_vocabulary / started_at).
def test_runner_supports_intent_vocabulary_hook(db: Session):
    fx = _seed_classification(
        db,
        n_feedback=2,
        feedback_contents=("alpha text", "beta text"),
        type_plan=("bug", "feature_request"),
        severity_plan=(3, 4),
    )
    dataset = _make_classification_dataset(
        feedbacks=fx.feedbacks,
        labels={
            "F001": {
                "feedback_type": "bug",
                "severity": 3,
            },
            "F002": {
                "feedback_type": "feature_request",
                "severity": 4,
            },
        },
    )
    run = evaluate_existing_run(
        db,
        project_id=fx.project.id,
        source_run_id=fx.run.id,
        evaluation_type="classification_metrics",
        dataset=dataset,
        intent_vocabulary=(),
        started_at=datetime.now(timezone.utc),
    )
    assert run.id is not None
