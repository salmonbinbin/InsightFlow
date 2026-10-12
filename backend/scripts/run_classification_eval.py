"""Phase 1 — Real classification evaluation.

Boots an isolated temporary PostgreSQL via ``pgserver``, runs the
classification pipeline against ``data/demo_feedback.csv`` using the real
LLM endpoint configured by ``LLM_*`` env vars, and prints an honest
metrics report. Labels in ``data/golden_dataset.csv`` are *initial
reference* and have not been human-approved; their accuracy numbers are
clearly marked.

The script NEVER logs API keys or writes them to disk. Results are printed
to stdout and to ``work/eval-results/<timestamp>.json``.

Usage::

    LLM_API_KEY=... LLM_BASE_URL=https://api.minimaxi.com/anthropic \\
    LLM_MODEL=MiniMax-M3 \\
    uv run --with pgserver==0.1.4 python scripts/run_classification_eval.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pgserver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

# Pull LLM env vars before importing app code so the Settings singleton picks them up.
API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("ANTHROPIC_MODEL")
if API_KEY and not os.environ.get("LLM_API_KEY"):
    os.environ["LLM_API_KEY"] = API_KEY
if BASE_URL and not os.environ.get("LLM_BASE_URL"):
    os.environ["LLM_BASE_URL"] = BASE_URL
if MODEL and not os.environ.get("LLM_MODEL"):
    os.environ["LLM_MODEL"] = MODEL

# Ensure backend root on sys.path so ``app.*`` imports resolve.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.prompts.feedback_classification_v1 import PROMPT_VERSION  # noqa: E402
from app.db.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    Feedback,
    FeedbackAnalysis,
    Project,
)
from app.services.analysis_runner import start_run  # noqa: E402
from app.services.classification import (  # noqa: E402
    ClassificationConfigError,
    is_classification_configured,
)

DATA = ROOT.parent / "data"
RESULTS_DIR = ROOT.parent / "work" / "eval-results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def load_demo_rows():
    rows = []
    with (DATA / "demo_feedback.csv").open(encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rows.append(row)
    return rows


def load_golden_labels():
    if not (DATA / "golden_dataset.csv").exists():
        return {}
    golden = {}
    with (DATA / "golden_dataset.csv").open(encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            golden[row["feedback_id"]] = row
    return golden


def make_project(db, name: str, product_name: str) -> Project:
    project = Project(name=name, product_name=product_name)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def import_feedback(db, project_id, rows):
    inserted = 0
    for row in rows:
        existing = db.scalar(
            select(Feedback).where(
                Feedback.project_id == project_id,
                Feedback.external_id == row["feedback_id"],
            )
        )
        if existing is not None:
            continue
        created_at = None
        if row.get("created_at"):
            try:
                created_at = datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
            except ValueError:
                created_at = None
        db.add(
            Feedback(
                project_id=project_id,
                external_id=row["feedback_id"],
                content=row["content"],
                source=row.get("source") or None,
                user_id=row.get("user_id") or None,
                user_type=row.get("user_type") or None,
                feedback_created_at=created_at,
            )
        )
        inserted += 1
    db.commit()
    return inserted


def feedback_type_accuracy(predictions, golden):
    if not golden:
        return None, 0, []
    correct = 0
    compared = 0
    misses = []
    for external_id, gt in golden.items():
        compared += 1
        pred = predictions.get(external_id)
        if pred is None:
            misses.append((external_id, gt.get("gt_feedback_type"), "no prediction"))
            continue
        if pred["feedback_type"] == gt.get("gt_feedback_type"):
            correct += 1
        else:
            misses.append(
                (
                    external_id,
                    gt.get("gt_feedback_type"),
                    pred["feedback_type"],
                )
            )
    return (correct / compared if compared else None), compared, misses


def severity_metrics(predictions, golden):
    if not golden:
        return None, None, 0, []
    abs_errors = []
    correct = 0
    compared = 0
    deltas = []
    for external_id, gt in golden.items():
        pred = predictions.get(external_id)
        if not gt.get("gt_severity"):
            continue
        gt_sev = int(gt["gt_severity"])
        compared += 1
        if pred is None:
            deltas.append((external_id, None, gt_sev))
            continue
        pred_sev = int(pred["severity"])
        abs_errors.append(abs(pred_sev - gt_sev))
        deltas.append((external_id, pred_sev, gt_sev))
        if pred_sev == gt_sev:
            correct += 1
    exact = (correct / compared) if compared else None
    mae = statistics.mean(abs_errors) if abs_errors else None
    return exact, mae, compared, deltas


def main():
    if not is_classification_configured():
        print(
            "ERROR: LLM_API_KEY / LLM_BASE_URL / LLM_MODEL not set. Cannot run a real evaluation.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"Model:  {settings.llm_model}")
    print("Base:   configured")
    print(f"Prompt: {PROMPT_VERSION}")
    print(f"Batch:  {settings.classification_batch_size}")
    print(f"Retry:  {settings.classification_max_retries}")
    print("")

    # Each evaluation owns a fresh PostgreSQL cluster. Never reuse a fixed
    # directory containing a previous run's data or migration history.
    with (
        tempfile.TemporaryDirectory(prefix="insightflow-eval-") as temp_dir,
        pgserver.get_server(Path(temp_dir) / "data", cleanup_mode="stop") as server,
    ):
        uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        os.environ["DATABASE_URL"] = uri
        env = {**os.environ}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            check=True,
        )
        engine = create_engine(uri)
        Session_ = sessionmaker(bind=engine)

        with Session_() as db:
            project = make_project(db, "Phase 1 Eval Project", "AI Assistant")
            rows = load_demo_rows()
            inserted = import_feedback(db, project.id, rows)
            print(f"Project {project.id} created; imported {inserted} feedback items.")

            t0 = time.monotonic()
            try:
                run = start_run(db, project.id)
            except ClassificationConfigError as exc:
                print(f"FAILED: {exc}")
                sys.exit(2)
            elapsed = time.monotonic() - t0

            analyses = db.scalars(
                select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == run.id)
            ).all()
            feedback_rows = db.scalars(select(Feedback)).all()
            feedback_by_uuid = {f.id: f for f in feedback_rows}
            feedback_rows_by_external = {f.external_id: f for f in feedback_rows}
            predictions = {}
            for analysis in analyses:
                fb = feedback_by_uuid.get(analysis.feedback_id)
                if fb is None:
                    continue
                predictions[fb.external_id] = {
                    "feedback_type": analysis.feedback_type,
                    "module": analysis.module,
                    "intent": analysis.intent,
                    "pain_point": analysis.pain_point,
                    "sentiment": analysis.sentiment,
                    "severity": analysis.severity,
                    "confidence": analysis.confidence,
                }

            golden = load_golden_labels()
            type_acc, type_compared, type_misses = feedback_type_accuracy(predictions, golden)
            sev_exact, sev_mae, sev_compared, sev_deltas = severity_metrics(predictions, golden)

            report = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": settings.llm_model,
                "prompt_version": run.prompt_version,
                "batch_size": settings.classification_batch_size,
                "max_retries": settings.classification_max_retries,
                "sample_size": len(rows),
                "dataset_sha256": {
                    name: hashlib.sha256((DATA / name).read_bytes()).hexdigest()
                    for name in ("demo_feedback.csv", "golden_dataset.csv")
                    if (DATA / name).exists()
                },
                "labels_status": (
                    "NOT human-reviewed; initial reference set" if golden else "no golden dataset"
                ),
                "run": {
                    "id": str(run.id),
                    "status": run.status,
                    "input_count": run.input_count,
                    "processed_count": run.processed_count,
                    "success_count": run.success_count,
                    "failed_count": run.failed_count,
                    "retry_count": run.retry_count,
                    "attempt_count": run.attempt_count,
                    "unknown_token_calls": run.unknown_token_calls,
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "latency_ms": run.latency_ms,
                    "elapsed_seconds": round(elapsed, 2),
                    "error_message": run.error_message,
                },
                "feedback_type_accuracy": {
                    "value": type_acc,
                    "compared": type_compared,
                    "misses": [
                        {"feedback_id": mid, "expected": exp, "got": got}
                        for mid, exp, got in type_misses
                    ],
                },
                "targets": {
                    "feedback_type_accuracy": 0.90,
                    "intent_accuracy": 0.85,
                    "pipeline_success": 0.95,
                },
                "actual": {
                    "pipeline_success": run.success_count / len(rows) if rows else None,
                    "feedback_type_accuracy": type_acc,
                    "intent_accuracy": None,
                    "intent_accuracy_limitation": "No human-approved scoring rubric; not evaluated",
                    "retry_rate_per_call": (
                        run.retry_count / run.attempt_count if run.attempt_count else None
                    ),
                    "schema_validated_rows": len(analyses),
                },
                "severity": {
                    "exact_accuracy": sev_exact,
                    "mae": sev_mae,
                    "compared": sev_compared,
                    "mae_compared": len([delta for delta in sev_deltas if delta[1] is not None]),
                    "deltas": [
                        {"feedback_id": fid, "pred": p, "gt": g} for fid, p, g in sev_deltas
                    ],
                },
                "predictions": predictions,
            }

            # Print human-readable summary.
            print("")
            print("=" * 60)
            print("Phase 1 — Real Classification Evaluation")
            print("=" * 60)
            print(f"Run status       : {run.status}")
            print(
                f"Pipeline success : {run.success_count}/{run.input_count} "
                f"({run.success_count / run.input_count:.0%})"
            )
            print(f"Retry count      : {run.retry_count}")
            print(f"input_tokens     : {run.input_tokens}")
            print(f"output_tokens    : {run.output_tokens}")
            print(f"latency_ms (sum) : {run.latency_ms}")
            print(f"elapsed_seconds  : {elapsed:.2f}")
            if type_acc is not None:
                print(
                    f"feedback_type acc (NOT human-reviewed labels): "
                    f"{type_acc:.0%} on {type_compared} compared"
                )
                for mid, exp, got in type_misses:
                    print(f"  miss {mid}: expected={exp!r} got={got!r}")
            else:
                print("feedback_type acc: not measured (no golden dataset)")
            if sev_exact is not None:
                mae_display = f"{sev_mae:.2f}" if sev_mae is not None else "unavailable"
                print(
                    f"severity exact acc (NOT human-reviewed labels): "
                    f"{sev_exact:.0%} on {sev_compared} compared; MAE={mae_display}"
                )
                for fid, p, g in sev_deltas:
                    marker = "OK" if p == g else "DIFF"
                    print(f"  {marker} {fid}: pred={p} gt={g}")
            print("")
            # Show raw content alongside prediction for manual review.
            for external_id in sorted(feedback_rows_by_external):
                fb = feedback_rows_by_external[external_id]
                p = predictions.get(external_id)
                pred_str = (
                    f"type={p['feedback_type']}, module={p['module']}, "
                    f"severity={p['severity']}, sentiment={p['sentiment']}, "
                    f"intent={p['intent']}, confidence={p['confidence']:.2f}"
                    if p
                    else "FAILED"
                )
                print(f"  {external_id}: {fb.content!r} -> {pred_str}")

            out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print("")
            print(f"Detailed report saved to: {out_path.relative_to(ROOT.parent)}")

            if run.status != "completed" or run.success_count != len(rows):
                print("ERROR: classification pipeline did not complete", file=sys.stderr)
                return 1

        server.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
