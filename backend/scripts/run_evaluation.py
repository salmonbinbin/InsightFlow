"""Phase 6 — developer CLI for re-running the Phase 6 evaluation harness.

This script is a thin wrapper around the in-process
:func:`app.evals.runner.evaluate_existing_run` + :func:`app.evals.regression.compare_metric_sets`
helpers. It does NOT re-implement metric math, does NOT call any LLM,
does NOT mutate Phase 1/2 source rows, and does NOT re-derive model
names from the CLI — ``model`` / ``prompt_version`` /
``pipeline_version`` are read from the source ``AnalysisRun`` that
already exists in the database. The CLI is purely a marshalling layer
plus a JSON report writer.

The CLI:

* Resolves the database URL from :class:`app.db.config.Settings`
  (i.e. the ``DATABASE_URL`` env var or the configured default) — it
  never accepts a URL on the command line so the runner's database
  binding stays the project's single source of truth.
* Loads the demo dataset snapshot via
  :func:`app.evals.dataset.load_demo_dataset`. The dataset is the only
  one supported by this CLI today; a future sprint can add more.
* Calls :func:`app.evals.runner.evaluate_existing_run` to score the
  source run against the snapshot. The runner does the real work; the
  CLI only marshals arguments, the report envelope, and the file /
  stdout write.
* Optionally calls :func:`app.evals.regression.compare_metric_sets` to
  diff the just-created run against a baseline evaluation run.
  Comparison is only attempted when the two runs share the same
  ``project_id`` + ``dataset_name`` + ``dataset_version`` +
  ``dataset_content_hash``; otherwise the report's
  ``comparison.eligible`` is ``false`` with a safe reason and the
  baseline row is never modified.
* Writes a UTF-8 JSON report to ``--output`` (when given) or stdout.
  On any failure the script exits non-zero and writes a safe error
  summary to stderr; no SQL fragments, parameter tuples, or
  credential-like tokens are ever echoed.

The report envelope mirrors what the API endpoints already return so a
developer running the CLI offline gets the same numbers / shape the
dashboard renders.

Usage::

    uv run python scripts/run_evaluation.py \\
        --project-id <UUID> --source-run-id <UUID> \\
        --evaluation-type classification_metrics \\
        --dataset demo

    uv run python scripts/run_evaluation.py \\
        --project-id <UUID> --source-run-id <UUID> \\
        --evaluation-type classification_metrics --dataset demo \\
        --compare-to <EVAL_RUN_UUID> \\
        --output work/reports/eval-2026-10-14.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from uuid import UUID

# Ensure backend root on sys.path so ``app.*`` imports resolve.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.config import settings  # noqa: E402
from app.evals.dataset import DatasetValidationError, load_demo_dataset  # noqa: E402
from app.evals.regression import (  # noqa: E402
    MetricSnapshot,
    compare_metric_sets,
)
from app.evals.runner import (  # noqa: E402
    SUPPORTED_EVALUATION_TYPES,
    EvaluationRunnerConfigError,
    EvaluationRunnerSourceMismatchError,
    EvaluationRunnerValidationError,
    evaluate_existing_run,
)
from app.models.analysis import AnalysisRun  # noqa: E402
from app.models.evaluation import (  # noqa: E402
    EvaluationCase,
    EvaluationMetric,
    EvaluationRun,
)

# ---- Exit codes -------------------------------------------------------------

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_DB_CONNECTION = 3
EXIT_SOURCE_VALIDATION = 4
EXIT_DATASET_VALIDATION = 5
EXIT_INTERNAL = 6

# ---- Constants --------------------------------------------------------------

# The CLI supports the demo dataset snapshot only. A future sprint can
# add more without changing the contract here.
SUPPORTED_DATASETS: tuple[str, ...] = ("demo",)

NOT_MEASURED_DISPLAY: str = "NOT MEASURED"

# Reused verbatim from the API layer so the CLI's stderr scrubbing is
# symmetric with the API. SQL / parameter / credential markers are
# stripped before the message reaches the operator.
_SQL_OR_PARAMS_MARKERS = re.compile(
    r"\[SQL:|\[parameters?[^\]]*\]|"
    r"\bSELECT\b|\bINSERT\s+INTO\b|\bUPDATE\s+\w+\b|\bDELETE\s+FROM\b|"
    r"\bFROM\s+\w+\b|\bWHERE\s+|\bJOIN\s+|\bVALUES\s*\(|"
    r"PRAGMA|BEGIN\s+TRANSACTION|COMMIT|ROLLBACK|"
    r"sqlite|postgresql|psycopg",
    re.IGNORECASE,
)
_SENSITIVE_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{6,}|"
    r"AKIA[0-9A-Z]{8,}|"
    r"x-api-key=[^\s,;]+|"
    r"api[_-]?key[=:][^\s,;]+|"
    r"bearer\s+[A-Za-z0-9._\-]{6,}|"
    r"password[=:][^\s,;]+|"
    r"secret[=:][^\s,;]+|"
    r"token[=:][A-Za-z0-9._\-]{6,})",
    re.IGNORECASE,
)
_SAFE_REASON_MAX_LEN = 200


# ---- Safe error scrubbing ---------------------------------------------------


def scrub_cli_error(exc: BaseException | str) -> str:
    """Return a SAFE one-line summary of an exception for the CLI stderr.

    Same rules as :mod:`app.api.evaluations` and the runner's
    :func:`app.evals.runner._safe_runner_error`: when SQL / parameter
    markers are present the raw reason is dropped entirely (the type
    name is the only thing kept). Credential-like tokens are redacted
    when no SQL marker fires. The result is a single line, truncated.
    """
    if isinstance(exc, str):
        name = "Error"
        raw = exc
    else:
        name = type(exc).__name__
        raw = ""
        if exc.args:
            first = exc.args[0]
            if isinstance(first, str):
                raw = first
    if not raw:
        return name
    if _SQL_OR_PARAMS_MARKERS.search(raw):
        # Drop the reason entirely — a partial scrub of a SQL fragment
        # still leaks structure and column names.
        return name
    sanitized = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", raw)
    sanitized = sanitized.replace("\x00", " ")
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > _SAFE_REASON_MAX_LEN:
        sanitized = sanitized[:_SAFE_REASON_MAX_LEN]
    return f"{name}: {sanitized}"


def _format_metric_value(value: float | None) -> str:
    if value is None:
        return NOT_MEASURED_DISPLAY
    return f"{float(value):.6f}"


# ---- Argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_evaluation",
        description=(
            "Re-run the Phase 6 evaluation harness against an existing "
            "Phase 1 (classification) or Phase 2 (clustering) run. The "
            "runner reads the demo dataset, scores the source run, and "
            "(optionally) diffs the result against a baseline evaluation "
            "run. The model / prompt / pipeline_version are read from "
            "the source run — they cannot be overridden on the CLI."
        ),
    )
    parser.add_argument(
        "--project-id",
        type=UUID,
        required=True,
        help="UUID of the project the source run belongs to.",
    )
    parser.add_argument(
        "--source-run-id",
        type=UUID,
        required=True,
        help="UUID of the Phase 1 classification or Phase 2 clustering run to score.",
    )
    parser.add_argument(
        "--evaluation-type",
        choices=tuple(SUPPORTED_EVALUATION_TYPES),
        required=True,
        help=(
            "'classification_metrics' scores a Phase 1 classification run; "
            "'cluster_metrics' scores a Phase 2 clustering run."
        ),
    )
    parser.add_argument(
        "--dataset",
        choices=SUPPORTED_DATASETS,
        default="demo",
        help="Dataset snapshot to score against (default: demo).",
    )
    parser.add_argument(
        "--compare-to",
        type=UUID,
        default=None,
        help=(
            "Optional baseline evaluation run UUID. Comparison is only "
            "attempted when the two runs share the same project + dataset "
            "name/version/content_hash; otherwise the report's "
            "comparison.eligible is false with a safe reason."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Optional file path to write the UTF-8 JSON report. When "
            "omitted the report is printed to stdout."
        ),
    )
    return parser


# ---- Report construction ---------------------------------------------------


def _build_metric_block(metric_rows: list[EvaluationMetric]) -> list[dict]:
    out: list[dict] = []
    for row in sorted(metric_rows, key=lambda r: (r.stage, r.metric_name, str(r.id))):
        out.append(
            {
                "name": row.metric_name,
                "kind": row.metric_kind,
                "stage": row.stage,
                "value": row.value,
                "value_display": _format_metric_value(row.value),
                "sample_count": int(row.sample_count),
            }
        )
    return out


def _build_failure_cases(case_rows: list[EvaluationCase]) -> list[dict]:
    """Return only the failing cases so the report stays compact.

    A case is "failing" when ``passed is False`` AND it has an
    ``error_type`` (the runner always sets one for failing cases; the
    case is filtered out of this list if the type is None). Hashes
    come from the row verbatim — the CLI never touches the
    ``input`` / ``expected`` / ``actual`` plaintext columns, which the
    framework keeps NULL by default.
    """
    out: list[dict] = []
    for row in sorted(case_rows, key=lambda r: (r.stage, r.case_id, str(r.id))):
        if row.passed is not False:
            continue
        if not row.error_type:
            continue
        out.append(
            {
                "case_id": row.case_id,
                "error_type": row.error_type,
                "stage": row.stage,
                "expected_hash": row.expected_hash,
                "actual_hash": row.actual_hash,
            }
        )
    return out


def _build_top_failure_patterns(case_rows: list[EvaluationCase]) -> list[dict]:
    """Count failing cases by ``error_type`` and return the sorted histogram.

    Sorted by count desc, then ``error_type`` asc for determinism. The
    counts come from REAL case rows — no synthetic errors are invented
    in the CLI.
    """
    failed_types: list[str] = []
    for row in case_rows:
        if row.passed is False and row.error_type:
            failed_types.append(row.error_type)
    if not failed_types:
        return []
    counter = Counter(failed_types)
    return [
        {"error_type": et, "count": count}
        for et, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _build_comparison_block(
    db: Session,
    *,
    candidate_run: EvaluationRun,
    baseline_run_id: UUID,
) -> dict:
    """Diff the candidate run against a baseline evaluation run.

    Eligibility gate: same ``project_id`` + ``dataset_name`` +
    ``dataset_version`` + ``dataset_content_hash``. When the gate
    fails the report's ``eligible`` is ``false`` with a safe
    ``ineligibility_reason`` and the baseline row is NEVER modified.
    Per-metric sample_count / metric_kind mismatches are still
    surfaced via :func:`compare_metric_sets` so the dashboard can
    flag them.
    """
    baseline_run = db.get(EvaluationRun, baseline_run_id)
    if baseline_run is None:
        return {
            "baseline_run_id": str(baseline_run_id),
            "candidate_run_id": str(candidate_run.id),
            "eligible": False,
            "ineligibility_reason": f"baseline evaluation run {baseline_run_id} not found",
            "dataset_name": candidate_run.dataset_name,
            "dataset_version": candidate_run.dataset_version,
            "metrics": [],
            "summary": {},
        }

    if baseline_run.project_id != candidate_run.project_id:
        return {
            "baseline_run_id": str(baseline_run.id),
            "candidate_run_id": str(candidate_run.id),
            "eligible": False,
            "ineligibility_reason": (
                "project mismatch: baseline belongs to a different project; "
                "cross-project comparison rejected"
            ),
            "dataset_name": candidate_run.dataset_name,
            "dataset_version": candidate_run.dataset_version,
            "metrics": [],
            "summary": {},
        }

    dataset_anchors = {
        "name": (baseline_run.dataset_name, candidate_run.dataset_name),
        "version": (baseline_run.dataset_version, candidate_run.dataset_version),
        "content_hash": (
            baseline_run.dataset_content_hash,
            candidate_run.dataset_content_hash,
        ),
    }
    mismatched: list[str] = []
    for axis, (b, c) in dataset_anchors.items():
        if b != c:
            mismatched.append(f"dataset_{axis} baseline={b!r} candidate={c!r}")
    if mismatched:
        return {
            "baseline_run_id": str(baseline_run.id),
            "candidate_run_id": str(candidate_run.id),
            "eligible": False,
            "ineligibility_reason": ("dataset anchor mismatch: " + "; ".join(mismatched)),
            "dataset_name": candidate_run.dataset_name,
            "dataset_version": candidate_run.dataset_version,
            "metrics": [],
            "summary": {},
        }

    baseline_metrics = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == baseline_run.id)
    ).all()
    candidate_metrics = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == candidate_run.id)
    ).all()

    def _to_snapshot(run: EvaluationRun, rows: list[EvaluationMetric]) -> list[MetricSnapshot]:
        return [
            MetricSnapshot(
                dataset_name=run.dataset_name,
                dataset_version=run.dataset_version,
                stage=row.stage,
                metric_name=row.metric_name,
                metric_kind=row.metric_kind,
                sample_count=int(row.sample_count),
                value=row.value,
                details=dict(row.details or {}),
                run_id=str(run.id),
                calculation_version=run.calculation_version,
            )
            for row in rows
        ]

    baseline_snaps = _to_snapshot(baseline_run, baseline_metrics)
    candidate_snaps = _to_snapshot(candidate_run, candidate_metrics)

    try:
        comparison = compare_metric_sets(
            baseline_snaps,
            candidate_snaps,
            baseline_run_id=str(baseline_run.id),
            candidate_run_id=str(candidate_run.id),
        )
    except ValueError as exc:
        # ``compare_metric_sets`` raises when baseline / candidate
        # reference different datasets OR when a metric_kind
        # mismatch surfaces per-row. The dataset axis was already
        # gated above, so a remaining raise means per-row kind
        # drift — surface that as ``ineligible`` with a safe
        # summary (no SQL / params / credentials can leak through
        # the comparison path itself, but we scrub anyway).
        return {
            "baseline_run_id": str(baseline_run.id),
            "candidate_run_id": str(candidate_run.id),
            "eligible": False,
            "ineligibility_reason": scrub_cli_error(exc),
            "dataset_name": candidate_run.dataset_name,
            "dataset_version": candidate_run.dataset_version,
            "metrics": [],
            "summary": {},
        }

    return {
        "baseline_run_id": str(baseline_run.id),
        "candidate_run_id": str(candidate_run.id),
        "eligible": True,
        "ineligibility_reason": None,
        "dataset_name": comparison.dataset_name,
        "dataset_version": comparison.dataset_version,
        "metrics": [
            {
                "metric_name": row.metric_name,
                "stage": row.stage,
                "metric_kind": row.metric_kind,
                "sample_count": int(row.sample_count),
                "baseline_value": row.baseline_value,
                "candidate_value": row.candidate_value,
                "delta": row.delta,
                "status": row.status,
                "notes": list(row.notes),
            }
            for row in comparison.metrics
        ],
        "summary": dict(comparison.summary),
    }


def _build_report(
    db: Session,
    *,
    evaluation_type: str,
    candidate_run: EvaluationRun,
    source_run: AnalysisRun,
    compare_to: UUID | None,
) -> dict:
    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == candidate_run.id)
    ).all()
    case_rows = db.scalars(
        select(EvaluationCase).where(EvaluationCase.run_id == candidate_run.id)
    ).all()

    # The runner has snapshotted model / prompt_version /
    # pipeline_version onto the EvaluationRun row, so the CLI can
    # surface them verbatim without re-reading the source for those
    # fields. The source AnalysisRun is still loaded for its id +
    # status so the report's ``source_run`` block matches what the
    # API endpoints expose.

    tokens_block = {
        "input_tokens": candidate_run.input_tokens,
        "output_tokens": candidate_run.output_tokens,
        "unknown_token_calls": int(candidate_run.unknown_token_calls or 0),
    }
    cost_value = candidate_run.total_cost_usd

    report: dict = {
        "evaluation_id": str(candidate_run.id),
        "status": candidate_run.status,
        "project_id": str(candidate_run.project_id),
        "evaluation_type": candidate_run.evaluation_type,
        "stage": candidate_run.stage,
        "calculation_version": candidate_run.calculation_version,
        "dataset": {
            "name": candidate_run.dataset_name,
            "version": candidate_run.dataset_version,
            "content_hash": candidate_run.dataset_content_hash,
            "provenance": candidate_run.dataset_provenance,
            "split": candidate_run.dataset_split,
            "label_status": candidate_run.dataset_label_status,
            "size": int(candidate_run.input_count),
        },
        "source_run": {
            "id": str(source_run.id),
            "model": candidate_run.model,
            "prompt_version": candidate_run.prompt_version,
            "pipeline_version": candidate_run.pipeline_version,
            "run_type": evaluation_type,
            "status": source_run.status,
        },
        "metrics": _build_metric_block(list(metric_rows)),
        "failure_cases": _build_failure_cases(list(case_rows)),
        "top_failure_patterns": _build_top_failure_patterns(list(case_rows)),
        "tokens": tokens_block,
        "latency_ms": candidate_run.latency_ms,
        "cost": cost_value,
        "comparison": None,
    }

    if compare_to is not None:
        report["comparison"] = _build_comparison_block(
            db, candidate_run=candidate_run, baseline_run_id=compare_to
        )
    return report


# ---- Main flow --------------------------------------------------------------


def _resolve_output_path(path: Path) -> Path:
    """Resolve ``--output`` to an absolute path and validate the parent.

    The parent directory must already exist; creating directories on
    behalf of the user would be a hidden side effect that can mask a
    typo in the path. The CLI does NOT touch Phase 1/2 source data,
    but the parent of the report file IS implicitly the operator's
    workspace — refusing to create it keeps the contract honest.
    """
    resolved = path.expanduser().resolve()
    if not resolved.parent.is_dir():
        raise FileNotFoundError(f"output parent directory does not exist: {resolved.parent}")
    return resolved


def _write_report(report: dict, output: Path | None) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if output is None:
        sys.stdout.write(payload)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    output.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dataset not in SUPPORTED_DATASETS:
        print(
            f"ERROR: unsupported dataset {args.dataset!r}; allowed={list(SUPPORTED_DATASETS)}",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS

    # Resolve --output up front so a bad path is reported before we
    # open a database connection. A missing parent directory is a
    # configuration mistake, not a runtime failure.
    try:
        output_path: Path | None = (
            _resolve_output_path(args.output) if args.output is not None else None
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {scrub_cli_error(exc)}", file=sys.stderr)
        return EXIT_BAD_ARGS

    # Build the engine from the project's settings (DATABASE_URL env
    # var or the configured default). The CLI never accepts a URL on
    # the command line so the runner's database binding stays the
    # project's single source of truth.
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
    except SQLAlchemyError as exc:
        print(f"ERROR: database connection failed: {scrub_cli_error(exc)}", file=sys.stderr)
        return EXIT_DB_CONNECTION

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with SessionLocal() as db:
            try:
                dataset = load_demo_dataset()
            except DatasetValidationError as exc:
                print(
                    f"ERROR: demo dataset validation failed: {scrub_cli_error(exc)}",
                    file=sys.stderr,
                )
                return EXIT_DATASET_VALIDATION

            try:
                run = evaluate_existing_run(
                    db,
                    project_id=args.project_id,
                    source_run_id=args.source_run_id,
                    evaluation_type=args.evaluation_type,
                    dataset=dataset,
                )
            except EvaluationRunnerSourceMismatchError as exc:
                details = "; ".join(exc.errors) if exc.errors else str(exc)
                print(
                    f"ERROR: source run validation failed: {scrub_cli_error(details)}",
                    file=sys.stderr,
                )
                return EXIT_SOURCE_VALIDATION
            except EvaluationRunnerValidationError as exc:
                details = "; ".join(exc.errors) if exc.errors else str(exc)
                print(
                    f"ERROR: dataset validation failed: {scrub_cli_error(details)}",
                    file=sys.stderr,
                )
                return EXIT_DATASET_VALIDATION
            except EvaluationRunnerConfigError as exc:
                print(
                    f"ERROR: configuration error: {scrub_cli_error(exc)}",
                    file=sys.stderr,
                )
                return EXIT_SOURCE_VALIDATION
            except SQLAlchemyError as exc:
                print(f"ERROR: database failure: {scrub_cli_error(exc)}", file=sys.stderr)
                return EXIT_DB_CONNECTION
            except BaseException as exc:  # defensive: never echo raw text
                print(f"ERROR: runner crashed: {scrub_cli_error(exc)}", file=sys.stderr)
                return EXIT_INTERNAL

            db.refresh(run)
            # The source AnalysisRun is needed for ``source_run.id``
            # / ``source_run.status`` in the report. The runner
            # already validated it (cross-project, run_type,
            # terminal status), so a fresh ``db.get`` is cheap and
            # guaranteed to find the row.
            source_run = db.get(AnalysisRun, args.source_run_id)
            if source_run is None:  # pragma: no cover - runner already gated
                print(
                    "ERROR: source run disappeared after evaluation; cannot build report",
                    file=sys.stderr,
                )
                return EXIT_INTERNAL
            report = _build_report(
                db,
                evaluation_type=args.evaluation_type,
                candidate_run=run,
                source_run=source_run,
                compare_to=args.compare_to,
            )

        _write_report(report, output_path)
        # A "failed" run still produced a row, but the script exits
        # non-zero so CI / supervisors can gate on status without
        # having to parse the JSON.
        if report.get("status") == "failed":
            print(
                f"ERROR: evaluation run finished with status=failed "
                f"(evaluation_id={report['evaluation_id']})",
                file=sys.stderr,
            )
            return EXIT_INTERNAL
        return EXIT_OK
    finally:
        engine.dispose()


__all__ = [
    "EXIT_OK",
    "EXIT_BAD_ARGS",
    "EXIT_DB_CONNECTION",
    "EXIT_SOURCE_VALIDATION",
    "EXIT_DATASET_VALIDATION",
    "EXIT_INTERNAL",
    "SUPPORTED_DATASETS",
    "scrub_cli_error",
    "build_parser",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
