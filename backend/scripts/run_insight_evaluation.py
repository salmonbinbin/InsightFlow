"""Phase 6 — developer CLI for running the insight evaluation.

This CLI is a thin marshalling layer over
:func:`app.evals.insight_runner.evaluate_insight_run`. It does NOT
re-implement metric math, does NOT call any LLM, does NOT mutate
Phase 1/2/3 source rows, and does NOT re-derive ``model`` /
``prompt_version`` from the command line — those values are read
from the source ``InsightRun`` that already exists in the database.

The CLI:

* Resolves the database URL from :class:`app.db.config.Settings`
  (``DATABASE_URL`` env var or the configured default). The CLI
  never accepts a URL on the command line so the runner's database
  binding stays the project's single source of truth.
* Calls :func:`app.evals.insight_runner.evaluate_insight_run` to
  score the source insight run as a DB-snapshot evaluation. The
  runner does the real work; the CLI only marshals arguments, the
  report envelope, and the file / stdout write.
* Writes a UTF-8 JSON report to ``--output`` (when given) or
  stdout. On any failure the script exits non-zero and writes a
  safe error summary to stderr; no SQL parameters or credential-like
  tokens are ever echoed.

Usage::

    uv run python scripts/run_insight_evaluation.py \\
        --project-id <UUID> --insight-run-id <UUID>

    uv run python scripts/run_insight_evaluation.py \\
        --project-id <UUID> --insight-run-id <UUID> \\
        --output work/reports/insight-eval-2026-10-14.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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
from app.evals.insight_runner import (  # noqa: E402
    DATASET_NAME_INSIGHT_DB_SNAPSHOT,
    EVALUATION_TYPE_INSIGHT,
    HUMAN_REVIEWED_METRICS,
    evaluate_insight_run,
)
from app.evals.runner import (  # noqa: E402
    EvaluationRunnerConfigError,
    EvaluationRunnerSourceMismatchError,
    EvaluationRunnerValidationError,
)
from app.models import InsightRun  # noqa: E402
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
EXIT_INTERNAL = 6

# Human-readable token used in the report when a metric row has no
# numeric value (NOT_MEASURED). Kept identical to the Phase 1/2 CLI
# so dashboards / supervisors can grep the same way.
NOT_MEASURED_DISPLAY: str = "NOT MEASURED"

# Reused verbatim from the Phase 1/2 CLI: SQL / parameter /
# credential markers are stripped before any message reaches stderr.
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

    Same rules as :mod:`app.evals.runner._safe_runner_error`: when
    SQL / parameter markers are present the raw reason is dropped
    entirely (the type name is the only thing kept). Credential-like
    tokens are redacted when no SQL marker fires. The result is a
    single line, truncated.
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
        # Drop the reason entirely — a partial scrub of a SQL
        # fragment still leaks structure and column names.
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
        prog="run_insight_evaluation",
        description=(
            "Run the Phase 6 insight evaluation against an existing "
            "Phase 3 InsightRun. The runner is read-only against "
            "Phase 1/2/3 source rows; it writes ONE EvaluationRun + N "
            "EvaluationMetric + N EvaluationCase rows. The model / "
            "prompt_version / token / latency values are read from the "
            "source InsightRun — they cannot be overridden on the CLI."
        ),
    )
    parser.add_argument(
        "--project-id",
        type=UUID,
        required=True,
        help="UUID of the project the InsightRun belongs to.",
    )
    parser.add_argument(
        "--insight-run-id",
        type=UUID,
        required=True,
        help="UUID of the Phase 3 InsightRun to score.",
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


def _build_case_summary(case_rows: list[EvaluationCase]) -> dict:
    """Return a compact per-decision summary for the report.

    A "decision" is the ``notes`` token persisted by the runner
    (``ok`` / ``missing_feedback`` / ``wrong_project`` /
    ``external_id_mismatch``). The CLI never echoes the raw
    ``input`` / ``expected`` / ``actual`` plaintext columns — the
    framework keeps them NULL by default.
    """
    total = len(case_rows)
    passed = sum(1 for c in case_rows if c.passed is True)
    failed = sum(1 for c in case_rows if c.passed is False)
    by_decision: dict[str, int] = {}
    for c in case_rows:
        key = c.notes or "<unknown>"
        by_decision[key] = by_decision.get(key, 0) + 1
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "by_decision": by_decision,
    }


def _build_report(
    db: Session,
    *,
    candidate_run: EvaluationRun,
    source_run: InsightRun,
) -> dict:
    metric_rows = db.scalars(
        select(EvaluationMetric).where(EvaluationMetric.run_id == candidate_run.id)
    ).all()
    case_rows = db.scalars(
        select(EvaluationCase).where(EvaluationCase.run_id == candidate_run.id)
    ).all()

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
            "expected_dataset_name": DATASET_NAME_INSIGHT_DB_SNAPSHOT,
        },
        "source_run": {
            "id": str(source_run.id),
            "model": candidate_run.model,
            "prompt_version": candidate_run.prompt_version,
            "pipeline_version": candidate_run.pipeline_version,
            "run_type": "insight",
            "status": source_run.status,
        },
        "metrics": _build_metric_block(list(metric_rows)),
        "case_summary": _build_case_summary(list(case_rows)),
        "tokens": {
            "input_tokens": candidate_run.input_tokens,
            "output_tokens": candidate_run.output_tokens,
            "unknown_token_calls": int(candidate_run.unknown_token_calls or 0),
        },
        "latency_ms": candidate_run.latency_ms,
        "human_reviewed_placeholders": {
            "metric_names": list(HUMAN_REVIEWED_METRICS),
            "value_required": False,
        },
    }
    return report


# ---- Output handling --------------------------------------------------------


def _resolve_output_path(path: Path) -> Path:
    """Resolve ``--output`` to an absolute path and validate the parent.

    The parent directory must already exist; creating directories on
    behalf of the user would be a hidden side effect that can mask
    a typo in the path. The CLI does NOT touch Phase 1/2/3 source
    data, but the parent of the report file IS implicitly the
    operator's workspace — refusing to create it keeps the contract
    honest.
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


# ---- Main flow --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.insight_run_id is None or args.project_id is None:
        print("ERROR: --project-id and --insight-run-id are required", file=sys.stderr)
        return EXIT_BAD_ARGS

    try:
        output_path: Path | None = (
            _resolve_output_path(args.output) if args.output is not None else None
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {scrub_cli_error(exc)}", file=sys.stderr)
        return EXIT_BAD_ARGS

    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
    except SQLAlchemyError as exc:
        print(f"ERROR: database connection failed: {scrub_cli_error(exc)}", file=sys.stderr)
        return EXIT_DB_CONNECTION

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with SessionLocal() as db:
            try:
                run = evaluate_insight_run(
                    db,
                    project_id=args.project_id,
                    insight_run_id=args.insight_run_id,
                )
            except EvaluationRunnerSourceMismatchError as exc:
                details = "; ".join(exc.errors) if exc.errors else str(exc)
                print(
                    f"ERROR: source insight run validation failed: {scrub_cli_error(details)}",
                    file=sys.stderr,
                )
                return EXIT_SOURCE_VALIDATION
            except EvaluationRunnerValidationError as exc:
                details = "; ".join(exc.errors) if exc.errors else str(exc)
                print(
                    f"ERROR: insight evaluation validation failed: {scrub_cli_error(details)}",
                    file=sys.stderr,
                )
                return EXIT_SOURCE_VALIDATION
            except EvaluationRunnerConfigError as exc:
                print(
                    f"ERROR: configuration error: {scrub_cli_error(exc)}",
                    file=sys.stderr,
                )
                return EXIT_SOURCE_VALIDATION
            except SQLAlchemyError as exc:
                print(
                    f"ERROR: database failure: {scrub_cli_error(exc)}",
                    file=sys.stderr,
                )
                return EXIT_DB_CONNECTION
            except BaseException as exc:  # defensive: never echo raw text
                print(
                    f"ERROR: runner crashed: {scrub_cli_error(exc)}",
                    file=sys.stderr,
                )
                return EXIT_INTERNAL

            db.refresh(run)
            source_run = db.get(InsightRun, args.insight_run_id)
            if source_run is None:  # pragma: no cover - runner already gated
                print(
                    "ERROR: source insight run disappeared after evaluation",
                    file=sys.stderr,
                )
                return EXIT_INTERNAL

            # Light sanity check on the just-persisted row's contract
            # so a future refactor of the runner cannot silently regress
            # the CLI's downstream contract. The runner already
            # validated these during the commit; we re-check after the
            # commit so the report is built from the persisted truth.
            if run.evaluation_type != EVALUATION_TYPE_INSIGHT:
                print(
                    "ERROR: persisted EvaluationRun has unexpected "
                    f"evaluation_type={run.evaluation_type!r}; "
                    f"expected {EVALUATION_TYPE_INSIGHT!r}",
                    file=sys.stderr,
                )
                return EXIT_INTERNAL

            report = _build_report(db, candidate_run=run, source_run=source_run)

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
    "EXIT_INTERNAL",
    "NOT_MEASURED_DISPLAY",
    "scrub_cli_error",
    "build_parser",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
