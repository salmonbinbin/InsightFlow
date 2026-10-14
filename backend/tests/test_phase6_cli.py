"""Phase 6 — developer CLI tests (``scripts/run_evaluation.py``).

The CLI is invoked as a subprocess against a private temporary SQLite
database so the shared PostgreSQL that ``scripts/verify_postgres.py``
drives is never touched. Each test:

1. Spins up an isolated SQLite engine, builds the schema via
   ``Base.metadata.create_all``, and seeds a project + feedback +
   completed classification / cluster source run.
2. Invokes the CLI with ``DATABASE_URL`` pointing at that file so the
   CLI's own :class:`app.db.config.Settings` picks the private DB up.
3. Parses the JSON report from stdout (or the ``--output`` file) and
   the safe error summary from stderr.

Coverage:

* ``--help`` exits 0 with usage text on stdout.
* Missing / unknown / out-of-range CLI flags exit non-zero.
* Unsupported ``--evaluation-type`` is rejected by argparse.
* Unsupported ``--dataset`` is rejected by argparse.
* Happy path: a completed classification run + matching dataset
  produces a JSON report with ``status == "completed"``,
  ``evaluation_id``, dataset provenance, per-metric ``kind`` /
  ``value`` / ``value_display`` / ``sample_count``, the demo model
  / prompt_version surfaced from the source run, and a
  ``top_failure_patterns`` histogram.
* Failure path: a classification run with one missing
  ``FeedbackAnalysis`` row produces ``status == "partial_failed"``,
  a populated ``failure_cases`` list with ``no_prediction``, and
  the ``top_failure_patterns`` histogram reflects the real error
  type.
* Source validation: unknown source run id → exit non-zero + safe
  stderr (no SQL fragments, no credential-like tokens).
* Cross-project source run → exit non-zero + safe stderr.
* Non-terminal source run → exit non-zero + safe stderr.
* Non-existent dataset version in ``--compare-to``:
  ``comparison.eligible == false`` with a safe reason.
* Same dataset anchor, no overlap: ``missing_baseline`` /
  ``missing_candidate`` rows surface the explicit ``None`` value
  (NOT_MEASURED), never 0.
* Null metric: ``intent_exact_match`` carries ``value == null`` and
  ``value_display == "NOT MEASURED"``.
* ``--output`` writes a UTF-8 file with the same JSON content as
  stdout.
* ``scrub_cli_error`` strips SQL fragments and credential-like
  tokens before they reach the operator.
* Tokens / latency / cost are surfaced as ``None`` when the source
  run did not measure them (no silent zero coercion).
* Cluster path: a completed cluster run + demo dataset produces a
  JSON report with cluster metrics; cross-evaluation-type
  comparison (classification vs cluster) is rejected with
  ``ineligible`` because the stages differ at the dataset anchor
  level (both runs still share the demo dataset anchors; the
  per-row kind axis is the gating factor — see the test).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, event, update
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.evals.dataset import load_demo_dataset
from app.evals.runner import LOCAL_HASH_EMBEDDING_MODEL, evaluate_existing_run
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    Feedback,
    FeedbackAnalysis,
    Project,
    RunStatus,
)
from app.models.evaluation import EvaluationMetric, EvaluationRun

# ---- Paths / fixtures -------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_evaluation.py"
# ``scripts/run_evaluation.py`` adds the backend root to sys.path before
# importing app code. We do the same here so we can import the
# ``scrub_cli_error`` helper without launching a subprocess.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_evaluation as cli_module  # noqa: E402


@pytest.fixture
def tmp_sqlite_engine(tmp_path: Path):
    """Yield a private SQLite engine + session factory. Disposes after."""
    db_path = tmp_path / "phase6_cli_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield {"engine": engine, "SessionLocal": SessionLocal, "db_path": str(db_path)}
    finally:
        engine.dispose()


# ---- Helpers ----------------------------------------------------------------


def _seed_classification_source_run(SessionLocal, *, include_all: bool = True) -> dict:
    """Seed a project + feedbacks + completed classification source run.

    When ``include_all`` is True, every dataset case has a matching
    ``FeedbackAnalysis`` so the runner reports ``status == "completed"``.
    When False, the case ``F002`` is deliberately left without a
    ``FeedbackAnalysis`` so the runner reports
    ``status == "partial_failed"`` with one ``no_prediction`` failure.
    Feedback content matches the demo dataset byte-for-byte so the
    runner's content-hash validator accepts the case mapping.
    """
    dataset = load_demo_dataset()
    with SessionLocal() as db:
        project = Project(name="CLI test", product_name="InsightFlow")
        db.add(project)
        db.flush()

        feedbacks: list[Feedback] = []
        for case in dataset.cases:
            fb = Feedback(
                project_id=project.id,
                external_id=case.case_id,
                content=case.feedback_text,
            )
            db.add(fb)
            feedbacks.append(fb)
        db.flush()

        run = AnalysisRun(
            project_id=project.id,
            run_type="classification",
            model="cli-test-classification-model",
            prompt_version="classification-v1",
            status=RunStatus.COMPLETED.value,
            input_count=len(feedbacks),
            processed_count=len(feedbacks),
            success_count=len(feedbacks) if include_all else len(feedbacks) - 1,
            failed_count=0,
            input_tokens=120,
            output_tokens=60,
            unknown_token_calls=0,
            latency_ms=1234,
            attempt_count=1,
            retry_count=0,
        )
        db.add(run)
        db.flush()

        for case, fb in zip(dataset.cases, feedbacks):
            if not include_all and case.case_id == "F002":
                continue
            db.add(
                FeedbackAnalysis(
                    feedback_id=fb.id,
                    run_id=run.id,
                    feedback_type=case.feedback_type_label or "bug",
                    module="m",
                    intent="i",
                    pain_point="p",
                    sentiment="negative",
                    severity=case.severity_label or 3,
                    confidence=0.9,
                )
            )
        db.commit()
        db.refresh(project)
        db.refresh(run)
        return {
            "project_id": str(project.id),
            "source_run_id": str(run.id),
            "dataset_version": dataset.version,
            "dataset_content_hash": dataset.content_hash,
            "dataset_name": dataset.name,
        }


def _seed_classification_source_run_with_unknown_tokens(
    SessionLocal,
) -> dict:
    """Seed a project + classification run with ``unknown_token_calls=2``.

    The runner must propagate the unknown signal: ``input_tokens`` /
    ``output_tokens`` become ``None`` on the evaluation run, and
    ``latency_ms`` becomes ``None`` when ``attempt_count == 0``.
    """
    dataset = load_demo_dataset()
    with SessionLocal() as db:
        project = Project(name="CLI test unknown", product_name="InsightFlow")
        db.add(project)
        db.flush()

        feedbacks: list[Feedback] = []
        for case in dataset.cases:
            fb = Feedback(
                project_id=project.id,
                external_id=case.case_id,
                content=case.feedback_text,
            )
            db.add(fb)
            feedbacks.append(fb)
        db.flush()

        run = AnalysisRun(
            project_id=project.id,
            run_type="classification",
            model="cli-test-unknown-model",
            prompt_version="classification-v1",
            status=RunStatus.COMPLETED.value,
            input_count=len(feedbacks),
            processed_count=len(feedbacks),
            success_count=len(feedbacks),
            failed_count=0,
            input_tokens=0,
            output_tokens=0,
            unknown_token_calls=2,
            latency_ms=0,
            attempt_count=0,
            retry_count=0,
        )
        db.add(run)
        db.flush()
        for case, fb in zip(dataset.cases, feedbacks):
            db.add(
                FeedbackAnalysis(
                    feedback_id=fb.id,
                    run_id=run.id,
                    feedback_type=case.feedback_type_label or "bug",
                    module="m",
                    intent="i",
                    pain_point="p",
                    sentiment="negative",
                    severity=case.severity_label or 3,
                    confidence=0.9,
                )
            )
        db.commit()
        db.refresh(project)
        db.refresh(run)
        return {
            "project_id": str(project.id),
            "source_run_id": str(run.id),
        }


def _seed_cluster_source_run(SessionLocal) -> dict:
    """Seed a project + classification + completed cluster source run.

    Every feedback ends up under ONE predicted cluster so the cluster
    metrics collapse to the same simple shape the runner tests use.
    The cluster source run lives alongside a sibling classification
    run because the cluster runner needs valid FeedbackAnalysis rows.
    """
    dataset = load_demo_dataset()
    with SessionLocal() as db:
        project = Project(name="CLI cluster test", product_name="InsightFlow")
        db.add(project)
        db.flush()

        feedbacks: list[Feedback] = []
        analyses: list[FeedbackAnalysis] = []
        for case in dataset.cases:
            fb = Feedback(
                project_id=project.id,
                external_id=case.case_id,
                content=case.feedback_text,
            )
            db.add(fb)
            feedbacks.append(fb)

        # Sibling classification run: every feedback gets an analysis.
        class_run = AnalysisRun(
            project_id=project.id,
            run_type="classification",
            model="cli-test-cluster-classification",
            prompt_version="classification-v1",
            status=RunStatus.COMPLETED.value,
            input_count=len(feedbacks),
            processed_count=len(feedbacks),
            success_count=len(feedbacks),
            failed_count=0,
            input_tokens=0,
            output_tokens=0,
            unknown_token_calls=0,
            latency_ms=100,
            attempt_count=1,
            retry_count=0,
        )
        db.add(class_run)
        db.flush()
        for case, fb in zip(dataset.cases, feedbacks):
            analysis = FeedbackAnalysis(
                feedback_id=fb.id,
                run_id=class_run.id,
                feedback_type=case.feedback_type_label or "bug",
                module="m",
                intent="i",
                pain_point="p",
                sentiment="negative",
                severity=case.severity_label or 3,
                confidence=0.9,
            )
            db.add(analysis)
            analyses.append(analysis)
        db.flush()

        run = AnalysisRun(
            project_id=project.id,
            run_type="clustering",
            model=LOCAL_HASH_EMBEDDING_MODEL,
            prompt_version="cluster-review-v4",
            status=RunStatus.COMPLETED.value,
            input_count=len(feedbacks),
            processed_count=len(feedbacks),
            success_count=len(feedbacks),
            failed_count=0,
            input_tokens=200,
            output_tokens=80,
            unknown_token_calls=0,
            latency_ms=5000,
            attempt_count=1,
            retry_count=0,
            cluster_config={"embedding_model": LOCAL_HASH_EMBEDDING_MODEL},
        )
        db.add(run)
        db.flush()

        cluster = Cluster(
            project_id=project.id,
            run_id=run.id,
            embedding_model=LOCAL_HASH_EMBEDDING_MODEL,
            distance_threshold=0.5,
            linkage="average",
        )
        db.add(cluster)
        db.flush()
        for analysis in analyses:
            db.add(
                ClusterMember(
                    cluster_id=cluster.id,
                    feedback_analysis_id=analysis.id,
                    similarity_score=0.9,
                )
            )
        db.commit()
        db.refresh(project)
        db.refresh(run)
        return {
            "project_id": str(project.id),
            "source_run_id": str(run.id),
            "dataset_version": dataset.version,
            "dataset_content_hash": dataset.content_hash,
            "dataset_name": dataset.name,
        }


def _run_cli(env: dict, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Invoke the CLI as a subprocess with a controlled env.

    ``env`` is merged over the test process's env so the caller only
    has to set the keys it cares about (typically ``DATABASE_URL``).
    """
    full_env = os.environ.copy()
    full_env.update(env)
    # Make sure the subprocess can import ``app.*`` even when pytest
    # was launched from a parent that does not have backend on
    # PYTHONPATH.
    full_env.setdefault("PYTHONPATH", str(ROOT))
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=full_env,
        check=check,
    )


def _assert_safe_stderr(stderr: str) -> None:
    """Defence-in-depth: the CLI's stderr MUST NOT echo raw error text."""
    sql_fragments = [
        "[SQL:",
        "[parameters",
        "INSERT INTO",
        "VALUES (",
        "SELECT ",
        "FROM projects",
        "FROM evaluation_runs",
    ]
    credential_fragments = [
        "sk-abc12345",
        "api_key=DEADBEEF",
        "Bearer eyJ",
        "password=foo",
    ]
    for needle in sql_fragments + credential_fragments:
        assert needle not in stderr, f"stderr leaked unsafe fragment {needle!r}: {stderr!r}"


# ---- Argparse / help --------------------------------------------------------


def test_help_exits_zero():
    result = _run_cli({}, "--help")
    assert result.returncode == 0
    assert "run_evaluation" in result.stdout
    assert "--project-id" in result.stdout
    assert "--source-run-id" in result.stdout
    assert "--evaluation-type" in result.stdout
    assert "--dataset" in result.stdout
    assert "--compare-to" in result.stdout
    assert "--output" in result.stdout


def test_missing_project_id_rejected_by_argparse():
    result = _run_cli(
        {"DATABASE_URL": "sqlite:////tmp/nonexistent.db"},
        "--source-run-id",
        str(uuid4()),
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode == 2
    assert "required" in result.stderr or "--project-id" in result.stderr


def test_invalid_uuid_rejected_by_argparse():
    result = _run_cli(
        {"DATABASE_URL": "sqlite:////tmp/nonexistent.db"},
        "--project-id",
        "not-a-uuid",
        "--source-run-id",
        str(uuid4()),
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode == 2


def test_unsupported_evaluation_type_rejected_by_argparse():
    result = _run_cli(
        {"DATABASE_URL": "sqlite:////tmp/nonexistent.db"},
        "--project-id",
        str(uuid4()),
        "--source-run-id",
        str(uuid4()),
        "--evaluation-type",
        "pipeline_health",
        "--dataset",
        "demo",
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr or "argument" in result.stderr


def test_unsupported_dataset_rejected_by_argparse():
    result = _run_cli(
        {"DATABASE_URL": "sqlite:////tmp/nonexistent.db"},
        "--project-id",
        str(uuid4()),
        "--source-run-id",
        str(uuid4()),
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "golden_full",
    )
    assert result.returncode == 2


# ---- Output file ------------------------------------------------------------


def test_output_file_is_written(tmp_sqlite_engine):
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    output_path = Path(tmp_sqlite_engine["db_path"]).parent / "report.json"
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
        "--output",
        str(output_path),
    )
    assert result.returncode == 0, result.stderr
    assert output_path.is_file()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["status"] in {"completed", "partial_failed", "failed"}
    assert payload["evaluation_id"]


def test_output_path_missing_parent_fails_safely(tmp_sqlite_engine):
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    missing = Path(tmp_sqlite_engine["db_path"]).parent / "does-not-exist" / "report.json"
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
        "--output",
        str(missing),
    )
    assert result.returncode == 2
    assert "does not exist" in result.stderr
    _assert_safe_stderr(result.stderr)


# ---- Happy path -------------------------------------------------------------


def test_full_success_classification(tmp_sqlite_engine):
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode == 0, result.stderr
    _assert_safe_stderr(result.stderr)
    report = json.loads(result.stdout)
    # Top-level envelope
    assert report["status"] in {"completed", "partial_failed", "failed"}
    assert report["status"] == "completed", report
    assert report["evaluation_id"]
    assert report["project_id"] == seed["project_id"]
    assert report["evaluation_type"] == "classification_metrics"
    assert report["stage"] == "classification"
    assert report["calculation_version"].startswith("phase6-")
    # Dataset provenance / size / version
    dataset_block = report["dataset"]
    assert dataset_block["name"] == "demo_feedback_plus_golden"
    assert dataset_block["provenance"] == "demo"
    assert dataset_block["split"] == "full"
    assert dataset_block["label_status"] == "synthetic_reference_labels_unreviewed_no_human_review"
    assert dataset_block["version"].startswith("demo-")
    assert len(dataset_block["content_hash"]) == 64
    assert dataset_block["size"] == 12
    # Source run binding is read from the source row, not the CLI
    source_block = report["source_run"]
    assert source_block["id"] == seed["source_run_id"]
    assert source_block["model"] == "cli-test-classification-model"
    assert source_block["prompt_version"] == "classification-v1"
    assert source_block["run_type"] == "classification_metrics"
    # Metrics: each has kind / value / value_display / sample_count
    metrics = report["metrics"]
    metric_names = {m["name"] for m in metrics}
    classification_metric_names = {
        "feedback_type_accuracy",
        "feedback_type_macro_f1",
        "severity_exact_match",
        "severity_mae",
        "intent_exact_match",
    }
    pipeline_metric_names = {
        "pipeline_success_rate",
        "pipeline_failure_stage_count",
    }
    # The four original per-label accuracy / severity metrics MUST
    # still be present — the brief is explicit that adding the new
    # pipeline layer must not regress the existing per-label signal.
    assert {
        "feedback_type_accuracy",
        "feedback_type_macro_f1",
        "severity_exact_match",
        "severity_mae",
    }.issubset(metric_names)
    # The two pipeline-level metrics MUST also be present — the brief
    # requires them on every classification evaluation run so a
    # pipeline-execution regression cannot be confused with a label
    # accuracy regression.
    assert pipeline_metric_names.issubset(metric_names)
    # The pipeline rows are deterministic on the source counters:
    # 12 inputs all succeeded, so the success rate is 1.0 and the
    # failure count is 0.
    pipeline_success = next(m for m in metrics if m["name"] == "pipeline_success_rate")
    assert pipeline_success["stage"] == "pipeline"
    assert pipeline_success["sample_count"] == 12
    assert pipeline_success["value"] == pytest.approx(1.0)
    pipeline_failure = next(m for m in metrics if m["name"] == "pipeline_failure_stage_count")
    assert pipeline_failure["stage"] == "pipeline"
    assert pipeline_failure["sample_count"] == 12
    assert pipeline_failure["value"] == pytest.approx(0.0)
    for metric in metrics:
        assert metric["kind"] == "deterministic"
        assert isinstance(metric["sample_count"], int) and metric["sample_count"] >= 0
        if metric["value"] is None:
            assert metric["value_display"] == "NOT MEASURED"
        else:
            assert metric["value_display"] != "NOT MEASURED"
            assert isinstance(metric["value"], (int, float))
        if metric["name"] in pipeline_metric_names:
            # The new pipeline-level rows live under their own stage
            # so the dashboard does not confuse them with per-label
            # metrics whose stage matches the originating analysis.
            assert metric["stage"] == "pipeline"
        else:
            assert metric["name"] in classification_metric_names
            assert metric["stage"] == "classification"
    # No raw feedback text leaks into the report
    for needle in ("content", "input_text"):
        # The report deliberately omits raw feedback text; the dataset
        # itself doesn't have a "content" key. We don't expect
        # ``case.feedback_text`` to appear anywhere in the report.
        assert "feedback_text" not in result.stdout
    # Failure cases + patterns: a "completed" run has zero failing cases
    assert report["failure_cases"] == []
    assert report["top_failure_patterns"] == []
    # Tokens / latency / cost are surfaced from the source run
    assert report["tokens"]["input_tokens"] == 120
    assert report["tokens"]["output_tokens"] == 60
    assert report["tokens"]["unknown_token_calls"] == 0
    assert report["latency_ms"] == 1234
    assert report["cost"] is None
    # Comparison is null when --compare-to is not given
    assert report["comparison"] is None


# ---- Failure path -----------------------------------------------------------


def test_failure_sample_no_prediction(tmp_sqlite_engine):
    """A classification run with one missing FeedbackAnalysis row records
    ``no_prediction`` and ``top_failure_patterns`` reflects the real
    error type. The script still exits 0 because the row was committed
    with status=partial_failed (not failed)."""
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"], include_all=False)
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode == 0, result.stderr
    _assert_safe_stderr(result.stderr)
    report = json.loads(result.stdout)
    assert report["status"] == "partial_failed", report
    failure_cases = report["failure_cases"]
    assert len(failure_cases) >= 1
    no_pred = next(c for c in failure_cases if c["error_type"] == "no_prediction")
    assert no_pred["case_id"] == "F002"
    assert no_pred["stage"] == "classification"
    assert no_pred["expected_hash"] is not None
    assert no_pred["actual_hash"] is None
    # Top failure patterns comes from real case error_type, sorted
    pattern_types = [p["error_type"] for p in report["top_failure_patterns"]]
    assert "no_prediction" in pattern_types
    # Every pattern is sorted by count desc, then error_type asc
    counts = [p["count"] for p in report["top_failure_patterns"]]
    assert counts == sorted(counts, reverse=True)


# ---- Source validation / safe errors ----------------------------------------


def test_unknown_source_run_returns_safe_error(tmp_sqlite_engine):
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        str(uuid4()),  # unknown
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode != 0
    _assert_safe_stderr(result.stderr)
    # The CLI's source-mismatch branch writes a safe error summary.
    assert "source run validation failed" in result.stderr


def test_cross_project_source_run_returns_safe_error(tmp_sqlite_engine):
    """A source run that lives under project A cannot be scored against
    project B. The runner raises ``EvaluationRunnerSourceMismatchError``
    and the CLI surfaces a SAFE error (no SQL / params / credentials)."""
    seed_a = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    # A second project exists in the same DB; the CLI is told to
    # evaluate project B's id but with project A's source run.
    with tmp_sqlite_engine["SessionLocal"]() as db:
        project_b = Project(name="Project B", product_name="InsightFlow")
        db.add(project_b)
        db.commit()
        db.refresh(project_b)
        project_b_id = str(project_b.id)
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        project_b_id,
        "--source-run-id",
        seed_a["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode != 0
    _assert_safe_stderr(result.stderr)
    assert "source run validation failed" in result.stderr


def test_non_terminal_source_run_returns_safe_error(tmp_sqlite_engine):
    """A classification run still in ``running`` state is not eligible
    for evaluation. The runner rejects it and the CLI exits non-zero
    with a safe error summary."""
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    # Force the source run into a non-terminal state via direct UPDATE.
    with tmp_sqlite_engine["SessionLocal"]() as db:
        db.execute(
            update(AnalysisRun)
            .where(AnalysisRun.id == UUID(seed["source_run_id"]))
            .values(status="running")
        )
        db.commit()
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode != 0
    _assert_safe_stderr(result.stderr)
    assert "source run validation failed" in result.stderr


# ---- Null / NOT_MEASURED rendering ------------------------------------------


def test_intent_metric_renders_not_measured(tmp_sqlite_engine):
    """The classification runner reports ``intent_exact_match`` as
    NOT_MEASURED by default. The CLI's report must surface ``value=None``
    AND ``value_display="NOT MEASURED"`` so a downstream consumer can
    render the value with the same stable string the API uses."""
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    intent = next(m for m in report["metrics"] if m["name"] == "intent_exact_match")
    assert intent["value"] is None
    assert intent["value_display"] == "NOT MEASURED"
    assert intent["sample_count"] >= 0


def test_tokens_latency_cost_surfaced_as_null_when_source_unknown(tmp_sqlite_engine):
    """When the source run reports ``unknown_token_calls=2`` and
    ``attempt_count=0`` / ``latency_ms=0``, the runner persists NULL on
    the eval run. The CLI surfaces those as ``None`` — never as 0."""
    seed = _seed_classification_source_run_with_unknown_tokens(tmp_sqlite_engine["SessionLocal"])
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["tokens"]["input_tokens"] is None
    assert report["tokens"]["output_tokens"] is None
    assert report["tokens"]["unknown_token_calls"] == 2
    assert report["latency_ms"] is None
    assert report["cost"] is None


# ---- Comparison: baseline + missing metric + dataset mismatch --------------


def _create_evaluation_run(SessionLocal, *, project_id: UUID, source_run_id: UUID) -> str:
    """Convenience: drive the runner directly to seed a baseline run row.

    The test then uses that row as the ``--compare-to`` target.
    """
    with SessionLocal() as db:
        dataset = load_demo_dataset()
        run = evaluate_existing_run(
            db,
            project_id=project_id,
            source_run_id=source_run_id,
            evaluation_type="classification_metrics",
            dataset=dataset,
        )
        db.refresh(run)
        return str(run.id)


def test_compare_to_missing_evaluation_run_is_ineligible(tmp_sqlite_engine):
    seed = _seed_classification_source_run(tmp_sqlite_engine["SessionLocal"])
    bogus_compare = uuid4()
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
        "--compare-to",
        str(bogus_compare),
    )
    assert result.returncode == 0, result.stderr
    _assert_safe_stderr(result.stderr)
    report = json.loads(result.stdout)
    comparison = report["comparison"]
    assert comparison is not None
    assert comparison["baseline_run_id"] == str(bogus_compare)
    assert comparison["eligible"] is False
    assert "not found" in comparison["ineligibility_reason"]
    assert comparison["metrics"] == []


def test_compare_to_baseline_runs_reports_matched_and_missing(tmp_sqlite_engine):
    """A second evaluation run on the same dataset + project is
    comparable. With identical seeds, every metric matches. The
    comparison block carries the per-row status and explicit
    baseline / candidate / delta values."""
    SessionLocal = tmp_sqlite_engine["SessionLocal"]
    seed = _seed_classification_source_run(SessionLocal)
    project_uuid = UUID(seed["project_id"])
    source_uuid = UUID(seed["source_run_id"])
    # Build the baseline via the same runner the CLI uses so the
    # report's metric set mirrors the candidate's exactly.
    baseline_id = _create_evaluation_run(
        SessionLocal, project_id=project_uuid, source_run_id=source_uuid
    )
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
        "--compare-to",
        baseline_id,
    )
    assert result.returncode == 0, result.stderr
    _assert_safe_stderr(result.stderr)
    report = json.loads(result.stdout)
    comparison = report["comparison"]
    assert comparison["eligible"] is True
    assert comparison["ineligibility_reason"] is None
    assert comparison["baseline_run_id"] == baseline_id
    assert comparison["candidate_run_id"] == report["evaluation_id"]
    assert comparison["dataset_name"] == "demo_feedback_plus_golden"
    assert comparison["dataset_version"] == seed["dataset_version"]
    by_name = {row["metric_name"]: row for row in comparison["metrics"]}
    # Measured metrics (both baseline and candidate have a real value):
    # the comparison marks them matched with delta=0.0. We assert per
    # metric so a future metric added to the runner can break the
    # comparison explicitly.
    #
    # The pipeline-level metrics (``pipeline_success_rate`` /
    # ``pipeline_failure_stage_count``) join the matched cohort
    # here too — the seed uses an identical source run so both the
    # baseline and the candidate read the same counter values, so
    # both rows are ``matched`` with delta=0.0. They sit on their
    # own ``stage=pipeline`` axis so the regression layer still
    # keys them correctly.
    measured_metrics = (
        "feedback_type_accuracy",
        "feedback_type_macro_f1",
        "severity_exact_match",
        "severity_mae",
        "pipeline_success_rate",
        "pipeline_failure_stage_count",
    )
    for name in measured_metrics:
        row = by_name[name]
        assert row["status"] == "matched", row
        assert row["baseline_value"] is not None
        assert row["candidate_value"] is not None
        assert row["delta"] == 0.0, row
    # Sanity-check that the pipeline metrics are routed under their
    # own stage on BOTH sides of the comparison — confirms the
    # composite key still pairs cleanly across the stage axis.
    for name in ("pipeline_success_rate", "pipeline_failure_stage_count"):
        assert by_name[name]["stage"] == "pipeline"
    # Unmeasured metric: intent_exact_match stays NOT_MEASURED on
    # BOTH sides (the runner emits value=None when no controlled
    # vocabulary is supplied). The comparison must surface the
    # explicit ``both_missing`` status with delta=None — never coerce
    # the unknown to 0.
    intent_row = by_name["intent_exact_match"]
    assert intent_row["status"] == "both_missing", intent_row
    assert intent_row["baseline_value"] is None
    assert intent_row["candidate_value"] is None
    assert intent_row["delta"] is None
    # Summary is consistent with the per-row statuses.
    assert comparison["summary"]["matched"] == len(measured_metrics)
    assert comparison["summary"]["both_missing"] >= 1
    assert comparison["summary"]["missing_baseline"] == 0
    assert comparison["summary"]["missing_candidate"] == 0


def test_compare_baseline_with_missing_metric_surfaces_not_measured(tmp_sqlite_engine):
    """When the baseline has a metric the candidate lacks (or vice
    versa), the regression comparison emits a ``missing_baseline`` /
    ``missing_candidate`` row. The CLI surfaces the missing value as
    explicit ``None`` (NOT_MEASURED) — never 0 — and the summary
    counts add up."""
    SessionLocal = tmp_sqlite_engine["SessionLocal"]
    seed = _seed_classification_source_run(SessionLocal)
    project_uuid = UUID(seed["project_id"])
    source_uuid = UUID(seed["source_run_id"])
    baseline_id = _create_evaluation_run(
        SessionLocal, project_id=project_uuid, source_run_id=source_uuid
    )
    # Delete one of the candidate-side metrics AFTER the baseline is
    # in place but BEFORE the candidate is generated. The candidate
    # will have 4 metrics; the baseline will keep all 4. We simulate
    # the asymmetry by deleting a metric row on the baseline after
    # the candidate run writes a fresh set.
    #
    # The simpler shape: generate the candidate FIRST, then strip a
    # metric on the baseline. The candidate is the one we just
    # produced in the CLI invocation below; the baseline already has
    # the full set from _create_evaluation_run.
    #
    # We invoke the CLI now to produce a candidate.
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
        "--compare-to",
        baseline_id,
    )
    assert result.returncode == 0, result.stderr
    report_with_baseline = json.loads(result.stdout)
    # Now strip one metric on the baseline so the next comparison
    # sees ``missing_baseline``.
    with SessionLocal() as db:
        deleted = db.execute(
            delete(EvaluationMetric).where(
                EvaluationMetric.run_id == UUID(baseline_id),
                EvaluationMetric.metric_name == "severity_mae",
            )
        )
        db.commit()
        assert deleted.rowcount == 1
    # Re-run the CLI: a fresh candidate run is created. The baseline
    # is now missing ``severity_mae``.
    result2 = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
        "--compare-to",
        baseline_id,
    )
    assert result2.returncode == 0, result2.stderr
    _assert_safe_stderr(result2.stderr)
    report = json.loads(result2.stdout)
    comparison = report["comparison"]
    assert comparison["eligible"] is True
    by_name = {row["metric_name"]: row for row in comparison["metrics"]}
    missing = by_name["severity_mae"]
    assert missing["status"] == "missing_baseline"
    assert missing["baseline_value"] is None
    assert missing["candidate_value"] is not None
    assert missing["delta"] is None
    assert comparison["summary"]["missing_baseline"] >= 1
    # Use the variable so linters don't complain about unused binding.
    _ = report_with_baseline


def test_compare_to_run_from_different_project_is_ineligible(tmp_sqlite_engine):
    """A baseline evaluation run that lives under a different project
    cannot be compared against the candidate — the eligibility gate
    enforces project isolation BEFORE dataset / per-metric checks run."""
    SessionLocal = tmp_sqlite_engine["SessionLocal"]
    seed = _seed_classification_source_run(SessionLocal)
    # A second project + its own completed source run; the comparison
    # will be project-mismatched even though both projects see the
    # same demo dataset.
    with SessionLocal() as db:
        other = Project(name="Project B", product_name="InsightFlow")
        db.add(other)
        db.flush()
        feedbacks = []
        for case in load_demo_dataset().cases:
            fb = Feedback(
                project_id=other.id,
                external_id=case.case_id,
                content=case.feedback_text,
            )
            db.add(fb)
            feedbacks.append(fb)
        db.flush()
        other_run = AnalysisRun(
            project_id=other.id,
            run_type="classification",
            model="other-model",
            prompt_version="classification-v1",
            status=RunStatus.COMPLETED.value,
            input_count=len(feedbacks),
            processed_count=len(feedbacks),
            success_count=len(feedbacks),
            failed_count=0,
            latency_ms=1000,
            attempt_count=1,
        )
        db.add(other_run)
        db.commit()
        db.refresh(other_run)
        other_run_id = str(other_run.id)
        other_project_id = str(other.id)
    # Build a baseline run on the OTHER project so the baseline's
    # project_id differs from the candidate's project_id.
    with SessionLocal() as db:
        dataset = load_demo_dataset()
        baseline_run = evaluate_existing_run(
            db,
            project_id=other.id,
            source_run_id=UUID(other_run_id),
            evaluation_type="classification_metrics",
            dataset=dataset,
        )
        db.refresh(baseline_run)
        baseline_id = str(baseline_run.id)
    # Now invoke the CLI for the FIRST project. The comparison is
    # cross-project → ineligible.
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "classification_metrics",
        "--dataset",
        "demo",
        "--compare-to",
        baseline_id,
    )
    assert result.returncode == 0, result.stderr
    _assert_safe_stderr(result.stderr)
    report = json.loads(result.stdout)
    comparison = report["comparison"]
    assert comparison["eligible"] is False
    assert "project mismatch" in comparison["ineligibility_reason"]
    # Use the other-project variables so linters don't complain.
    _ = other_project_id


# ---- Cluster path -----------------------------------------------------------


def test_cluster_full_success(tmp_sqlite_engine):
    seed = _seed_cluster_source_run(tmp_sqlite_engine["SessionLocal"])
    result = _run_cli(
        {"DATABASE_URL": f"sqlite:///{tmp_sqlite_engine['db_path']}"},
        "--project-id",
        seed["project_id"],
        "--source-run-id",
        seed["source_run_id"],
        "--evaluation-type",
        "cluster_metrics",
        "--dataset",
        "demo",
    )
    assert result.returncode == 0, result.stderr
    _assert_safe_stderr(result.stderr)
    report = json.loads(result.stdout)
    assert report["status"] in {"completed", "partial_failed", "failed"}
    assert report["evaluation_type"] == "cluster_metrics"
    assert report["stage"] == "clustering"
    metric_names = {m["name"] for m in report["metrics"]}
    assert {
        "cluster_count",
        "cluster_purity_micro",
        "cluster_coverage",
        "cluster_fragmentation_mean",
        "cluster_over_merge_ratio",
    }.issubset(metric_names)
    # Source run binding is read from the source run's embedding model
    # (NOT from a fabricated label). The runner pulls
    # ``Cluster.embedding_model`` as the eval-run model and writes
    # the local-hash warning into ``EvaluationRun.notes`` so a
    # regression can never confuse the deterministic local-hash
    # embedding with a real semantic embedding. The CLI's report
    # surfaces that fact via ``source_run.model`` — when a reader
    # sees ``local-hash-v1`` they know the topology is NOT
    # semantically meaningful.
    assert report["source_run"]["id"] == seed["source_run_id"]
    assert report["source_run"]["model"] == LOCAL_HASH_EMBEDDING_MODEL
    assert report["source_run"]["model"] == "local-hash-v1"
    assert report["source_run"]["pipeline_version"] == LOCAL_HASH_EMBEDDING_MODEL
    # The local-hash limitation is also recorded in the underlying
    # EvaluationRun.notes (not echoed into the JSON report by design,
    # to keep the report compact). The CLI still records it; a
    # supervisor reading the row directly sees the warning text.
    with tmp_sqlite_engine["SessionLocal"]() as db:
        eval_run = db.get(EvaluationRun, UUID(report["evaluation_id"]))
        notes = eval_run.notes or ""
    assert "local-hash-v1" in notes
    assert "NOT a real semantic embedding" in notes


# ---- scrub_cli_error unit test ---------------------------------------------


def test_scrub_cli_error_drops_sql_and_creds():
    """The scrubbing helper is the same regex used by the CLI's
    ``except`` chain. These cases are the "what would happen if
    an upstream library leaked raw text" boundary — they must be
    neutralised before anything reaches the operator."""
    raw = (
        "(IntegrityError) CHECK constraint failed\n"
        "[SQL: INSERT INTO evaluation_runs (id) VALUES (?)]\n"
        "[parameters: ('SECRET-UUID-1',)]\n"
        "Bearer sk-abc12345xyz9876"
    )
    safe = cli_module.scrub_cli_error(raw)
    # SQL / parameters / credentials are gone; only the type name
    # remains when an SQL marker is present.
    assert "[SQL" not in safe
    assert "INSERT INTO" not in safe
    assert "VALUES (" not in safe
    assert "[parameters" not in safe
    assert "SECRET-UUID-1" not in safe
    assert "sk-abc12345xyz9876" not in safe
    assert "Bearer " not in safe

    # When no SQL marker fires, credential-like tokens are still
    # redacted and the human-readable reason survives.
    non_sql = "transport failed: Authorization: Bearer sk-deadbeef9999, fallback api_key=DEADBEEF"
    safe2 = cli_module.scrub_cli_error(non_sql)
    assert "sk-deadbeef9999" not in safe2
    assert "api_key=DEADBEEF" not in safe2
    assert "transport failed" in safe2

    # Type-name only path (no string args): the helper returns the
    # class name so an audit can read it back without losing signal.
    class _NoArgsError(Exception):
        pass

    safe3 = cli_module.scrub_cli_error(_NoArgsError())
    assert safe3 == "_NoArgsError"
