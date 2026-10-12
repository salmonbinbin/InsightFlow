"""Phase 2 — Clustering evaluation with a threshold sweep.

Boots an isolated temporary PostgreSQL via ``pgserver``, imports
``data/demo_feedback.csv``, then runs the clustering pipeline at multiple
distance thresholds and computes four cluster-quality metrics against the
``gt_cluster`` labels in ``data/golden_dataset.csv``:

* **Cluster Purity** — average fraction of majority-class items in each
  predicted cluster. Higher is better.
* **Coverage** — fraction of GT clusters that are recovered by a single
  predicted cluster containing at least half of the GT cluster's members.
  Higher is better.
* **Fragmentation** — average number of predicted clusters each GT cluster
  is split across. Lower is better.
* **Over-merge** — fraction of predicted clusters that mix two or more
  GT clusters. Lower is better.

Two modes are supported via ``--mode``:

* ``full`` (default) — runs the real Phase 1 LLM classification pipeline
  first so the ``semantic_text`` and embeddings come from a real model.
  Requires ``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL``.
* ``synthetic`` — skips the LLM, writes ``semantic_text`` directly from
  the GT intent and pain_point so the threshold sweep isolates the
  clustering algorithm from Phase 1 noise. No external service required.

In both modes the LLM cluster review is **disabled** so the metrics reflect
the algorithmic threshold choice only. Embedding uses the local
deterministic hash embedder; the threshold sweep is what we want to study.

The script NEVER logs API keys. Results are written to
``work/eval-results/<timestamp>.json`` and a summary is printed to stdout.

Usage::

    # Threshold sweep with synthetic semantic_text (fast, no LLM):
    uv run --with pgserver==0.1.4 python scripts/run_clustering_eval.py

    # Threshold sweep with real Phase 1 LLM:
    LLM_API_KEY=... LLM_BASE_URL=https://api.minimaxi.com/anthropic \\
    LLM_MODEL=MiniMax-M3 \\
    uv run --with pgserver==0.1.4 python scripts/run_clustering_eval.py --mode full
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pgserver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

# Ensure backend root on sys.path so ``app.*`` imports resolve.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Pull LLM env vars before importing app code so the Settings singleton picks them up.
for _src, _dst in (
    ("ANTHROPIC_AUTH_TOKEN", "LLM_API_KEY"),
    ("ANTHROPIC_BASE_URL", "LLM_BASE_URL"),
    ("ANTHROPIC_MODEL", "LLM_MODEL"),
):
    _val = os.environ.get(_src)
    if _val and not os.environ.get(_dst):
        os.environ[_dst] = _val

from app.db import config as db_config  # noqa: E402
from app.models import (  # noqa: E402
    AnalysisRun,
    Cluster,
    ClusterMember,
    ClusterSource,
    Feedback,
    FeedbackAnalysis,
    Project,
    RunStatus,
)
from app.services.cluster_runner import (  # noqa: E402
    ClusteringConfigError,
    start_cluster_run,
)
from app.services.embedding import (  # noqa: E402
    EmbeddingStats,
    LocalHashEmbedder,
    ensure_embeddings,
)

DATA = ROOT.parent / "data"
RESULTS_DIR = ROOT.parent / "work" / "eval-results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_THRESHOLDS = (0.30, 0.45, 0.60)


# ----- Demo data ----------------------------------------------------------------


def load_demo_rows() -> list[dict]:
    with (DATA / "demo_feedback.csv").open(encoding="utf-8-sig") as fp:
        return list(csv.DictReader(fp))


def load_golden_clusters() -> dict[str, str]:
    """Map ``feedback_id → gt_cluster`` for every row in the golden set."""
    if not (DATA / "golden_dataset.csv").exists():
        return {}
    out: dict[str, str] = {}
    with (DATA / "golden_dataset.csv").open(encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            if row.get("gt_cluster"):
                out[row["feedback_id"]] = row["gt_cluster"]
    return out


def load_golden_full() -> dict[str, dict]:
    if not (DATA / "golden_dataset.csv").exists():
        return {}
    out: dict[str, dict] = {}
    with (DATA / "golden_dataset.csv").open(encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            out[row["feedback_id"]] = row
    return out


# ----- Pipeline plumbing --------------------------------------------------------


def make_project(db, name: str, product_name: str) -> Project:
    project = Project(name=name, product_name=product_name)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def import_feedback(db, project_id, rows) -> int:
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
                created_at = datetime.fromisoformat(row["created_at"]).replace(
                    tzinfo=timezone.utc
                )
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


def _synthetic_semantic_text(feedback: Feedback, golden_row: dict | None) -> str:
    """Build ``semantic_text`` for the synthetic mode.

    Uses the GT intent / pain_point so the embedding signal carries the
    ground-truth cluster label. The clustering algorithm then has to decide
    where to draw the threshold; the metrics quantify that decision.
    """
    if golden_row is None:
        return f"topic {feedback.content}"
    intent = (golden_row.get("gt_intent") or "unknown").strip()
    pain = (golden_row.get("gt_module") or "general").strip()
    return f"intent={intent} pain={pain}"


def _seed_synthetic_analyses(
    db, project_id, feedback_rows, golden: dict[str, dict]
) -> AnalysisRun:
    """Write a successful classification run + analyses without calling the LLM.

    The run row is marked ``completed`` so the cluster runner accepts it as
    the latest successful classification run. The semantic_text is derived
    from the GT labels so the embeddings carry a clean signal.

    ``feedback_rows`` may be ``Feedback`` ORM rows (have ``.id`` and
    ``.external_id``) or the raw CSV dicts (have ``external_id`` but not
    ``id``) — the function looks up the live ``Feedback`` row by
    ``external_id`` so the analyses link to real UUIDs.
    """
    run = AnalysisRun(
        project_id=project_id,
        run_type="classification",
        model="synthetic-eval",
        prompt_version="synthetic-eval-v1",
        status=RunStatus.COMPLETED.value,
        input_count=len(feedback_rows),
        processed_count=len(feedback_rows),
        success_count=len(feedback_rows),
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    for row in feedback_rows:
        ext_id = getattr(row, "external_id", None) or row["feedback_id"]
        feedback = db.scalar(
            select(Feedback).where(
                Feedback.project_id == project_id,
                Feedback.external_id == ext_id,
            )
        )
        if feedback is None:
            continue
        golden_row = golden.get(ext_id, {})
        db.add(
            FeedbackAnalysis(
                feedback_id=feedback.id,
                run_id=run.id,
                feedback_type=golden_row.get("gt_feedback_type") or "other",
                module=golden_row.get("gt_module") or "other",
                intent=golden_row.get("gt_intent") or "unknown",
                pain_point=golden_row.get("gt_intent") or "unknown",
                sentiment="negative",
                severity=int(golden_row.get("gt_severity") or 3),
                confidence=0.9,
                semantic_text=_synthetic_semantic_text(feedback, golden_row),
            )
        )
    db.commit()
    return run


def _ensure_embeddings_standalone(
    db, project_id, classification_run, embedder
) -> None:
    """Force ``ensure_embeddings`` to run against the synthetic analyses.

    The cluster runner calls ``ensure_embeddings`` itself; this helper is
    used by the synthetic mode to pre-populate embeddings and surface
    failures early. The cluster runner is still idempotent so the in-run
    call is a no-op.
    """
    analyses = db.scalars(
        select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == classification_run.id)
    ).all()
    feedback_by_id = {
        f.id: f
        for f in db.scalars(select(Feedback).where(Feedback.project_id == project_id)).all()
    }
    items = [
        (a, feedback_by_id[a.feedback_id])
        for a in analyses
        if a.feedback_id in feedback_by_id
    ]
    stats = EmbeddingStats()
    ensure_embeddings(db, classification_run, items, embedder=embedder, stats=stats)
    return stats


def _force_local_embedder() -> LocalHashEmbedder:
    """Disable remote embeddings and return the deterministic local embedder."""
    db_config.settings.embedding_model = "local-hash-eval-v1"
    db_config.settings.embedding_dimension = 128
    return LocalHashEmbedder("local-hash-eval-v1", 128)


def _disable_llm_review() -> None:
    """Make sure the cluster runner does not invoke the LLM review step."""
    db_config.settings.cluster_review_enabled = False


# ----- Metric computation ------------------------------------------------------


def _load_predicted_clusters(
    db, run_id: UUID
) -> list[tuple[str, list[str], str]]:
    """Return ``[(cluster_label, [external_feedback_id...], source)]``.

    ``cluster_label`` is either the LLM-assigned ``name`` (when review ran
    and named the cluster) or the cluster's UUID truncated to 8 chars (when
    only the algorithmic pass is in play). Either way, predicted clusters
    are distinguished by their identity, not the label.

    Outlier children are excluded — they are semantically "this does not
    belong in the main cluster", so merging them with the parent would
    double-count.
    """
    rows: list[tuple[str, list[str], str]] = []
    clusters = db.scalars(select(Cluster).where(Cluster.run_id == run_id)).all()
    for cluster in clusters:
        if cluster.source == ClusterSource.OUTLIER.value:
            continue
        members = db.scalars(
            select(ClusterMember).where(ClusterMember.cluster_id == cluster.id)
        ).all()
        analysis_ids = [m.feedback_analysis_id for m in members]
        analyses = db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.id.in_(analysis_ids))
        ).all()
        ext_ids: list[str] = []
        for a in analyses:
            fb = db.get(Feedback, a.feedback_id)
            if fb is not None:
                ext_ids.append(fb.external_id)
        if not ext_ids:
            continue
        label = cluster.name or f"cluster-{str(cluster.id)[:8]}"
        rows.append((label, sorted(ext_ids), cluster.source))
    return rows


def _safe_mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def compute_metrics(
    predicted: list[tuple[str, list[str], str]],
    golden: dict[str, str],
) -> dict:
    """Compute purity / coverage / fragmentation / over-merge for one run.

    GT labels with no predicted members are still counted in coverage
    ("uncovered" GT clusters) and fragmentation (contribute 0 splits
    because they have no predicted members — but this is the *desired*
    "no false-positive fragmentation" reading, not "this GT is split").
    """
    gt_labels_in_play = sorted(set(golden.values()))
    n_gt = len(gt_labels_in_play)
    n_pred = len(predicted)
    n_items = sum(len(members) for _, members, _ in predicted)

    if n_pred == 0 or n_items == 0:
        return {
            "n_predicted_clusters": 0,
            "n_gt_clusters": n_gt,
            "n_items": 0,
            "purity_micro": None,
            "purity_macro": None,
            "coverage": 0.0,
            "fragmentation_mean": 0.0,
            "over_merge_ratio": None,
            "predictions": [],
        }

    # Predicted cluster → list of GT labels for its members.
    cluster_to_gt: list[list[str]] = []
    for _, members, _ in predicted:
        gt_for_members = [golden.get(m) for m in members]
        gt_for_members = [g for g in gt_for_members if g is not None]
        cluster_to_gt.append(gt_for_members)

    # Purity: each predicted cluster's most common GT label, divided by size.
    cluster_purities: list[float] = []
    majority_count = 0
    for gts in cluster_to_gt:
        if not gts:
            continue
        counts = Counter(gts)
        top, top_n = counts.most_common(1)[0]
        majority_count += top_n
        cluster_purities.append(top_n / len(gts))
    purity_micro = majority_count / n_items if n_items else None
    purity_macro = _safe_mean(cluster_purities)

    # Over-merge: predicted clusters that contain members from ≥2 distinct
    # GT labels. Pure algorithmic clusters that map 1-to-1 with a GT label
    # have ratio 0; clusters mixing labels inflate the ratio.
    over_merge_count = sum(1 for gts in cluster_to_gt if len(set(gts)) >= 2)
    over_merge_ratio = over_merge_count / n_pred

    # Per-GT fragmentation: number of distinct predicted clusters that
    # contain ≥1 member of that GT label.
    gt_to_clusters: dict[str, set[int]] = defaultdict(set)
    for idx, gts in enumerate(cluster_to_gt):
        for g in set(gts):
            gt_to_clusters[g].add(idx)
    fragmentation_per_gt = [len(gt_to_clusters.get(g, set())) for g in gt_labels_in_play]
    fragmentation_mean = _safe_mean(fragmentation_per_gt)

    # Coverage: GT label is "recovered" if at least one predicted cluster
    # contains ≥50% of that GT label's total members.
    gt_total_counts = Counter(golden.values())
    recovered = 0
    for g in gt_labels_in_play:
        total = gt_total_counts[g]
        if total == 0:
            continue
        best = max(
            (Counter(gts).get(g, 0) for gts in cluster_to_gt),
            default=0,
        )
        if best / total >= 0.5:
            recovered += 1
    coverage = recovered / n_gt if n_gt else 0.0

    predictions_serialized = [
        {
            "cluster_label": label,
            "source": source,
            "member_count": len(members),
            "members": members,
            "gt_label_distribution": dict(Counter(golden.get(m) for m in members)),
        }
        for label, members, source in predicted
    ]

    return {
        "n_predicted_clusters": n_pred,
        "n_gt_clusters": n_gt,
        "n_items": n_items,
        "purity_micro": purity_micro,
        "purity_macro": purity_macro,
        "coverage": coverage,
        "fragmentation_mean": fragmentation_mean,
        "over_merge_ratio": over_merge_ratio,
        "predictions": predictions_serialized,
    }


# ----- Main ---------------------------------------------------------------------


def _format_pct(value: float | None) -> str:
    if value is None:
        return "  N/A "
    return f"{value:5.1%}"


def _format_float(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "  N/A "
    return f"{value:6.{digits}f}"


def _print_table(per_threshold: dict) -> None:
    print("")
    print("=" * 78)
    print("Phase 2 — Clustering Threshold Sweep")
    print("=" * 78)
    header = (
        f"{'Threshold':>10}  {'#Pred':>6}  {'#GT':>4}  "
        f"{'Purity μ':>8}  {'Purity M':>8}  {'Coverage':>9}  "
        f"{'Fragment':>9}  {'OverMerge':>10}"
    )
    print(header)
    print("-" * len(header))
    for key in sorted(per_threshold, key=lambda k: float(k)):
        m = per_threshold[key]
        threshold = float(key)
        print(
            f"{threshold:>10.2f}  {m['n_predicted_clusters']:>6d}  "
            f"{m['n_gt_clusters']:>4d}  "
            f"{_format_pct(m['purity_micro'])}  "
            f"{_format_pct(m['purity_macro'])}  "
            f"{_format_pct(m['coverage'])}  "
            f"{_format_float(m['fragmentation_mean'])}  "
            f"{_format_pct(m['over_merge_ratio'])}"
        )
    print("-" * len(header))
    print("Purity μ  = micro (Σ majority / Σ members)   (higher is better)")
    print("Purity M  = macro (avg of per-cluster purity) (higher is better)")
    print("Coverage  = GT clusters recovered by ≥50% (higher is better)")
    print("Fragment  = mean # predicted clusters per GT (lower is better)")
    print("OverMerge = predicted clusters mixing ≥2 GT labels (lower is better)")
    print("")


def _print_per_threshold_detail(per_threshold: dict) -> None:
    for key in sorted(per_threshold, key=lambda k: float(k)):
        m = per_threshold[key]
        print(f"--- threshold = {float(key):.2f} ---")
        for pred in m["predictions"]:
            dist = pred["gt_label_distribution"]
            dist_str = ", ".join(f"{k}={v}" for k, v in sorted(dist.items()))
            print(
                f"  {pred['source']:>11}  members={pred['member_count']:>2}  "
                f"label={pred['cluster_label']!r:>32}  GT={dist_str}"
            )
        print("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("synthetic", "full"),
        default="synthetic",
        help="synthetic skips the LLM (fast); full uses real Phase 1 LLM",
    )
    parser.add_argument(
        "--thresholds",
        nargs="+",
        type=float,
        default=list(DEFAULT_THRESHOLDS),
        help="distance thresholds to sweep (cosine distance, 0–1)",
    )
    parser.add_argument(
        "--linkage",
        default="average",
        choices=("average", "complete", "single"),
        help="agglomerative linkage to use",
    )
    args = parser.parse_args(argv)

    if args.mode == "full":
        from app.services.analysis_runner import start_run  # noqa: WPS433
        from app.services.classification import (  # noqa: WPS433
            ClassificationConfigError,
            is_classification_configured,
        )

        if not is_classification_configured():
            print(
                "ERROR: --mode full requires LLM_API_KEY / LLM_BASE_URL / LLM_MODEL.",
                file=sys.stderr,
            )
            return 2

    print(f"Mode:     {args.mode}")
    print(f"Linkage:  {args.linkage}")
    print(f"Thresholds: {args.thresholds}")
    print("Embedding: local-hash-eval-v1 (dim=128) — deterministic, no remote call")
    print("LLM review: disabled (algorithmic-only metrics)")
    print("")

    golden = load_golden_clusters()
    if not golden:
        print("ERROR: data/golden_dataset.csv missing or empty.", file=sys.stderr)
        return 2

    report: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "linkage": args.linkage,
        "embedding_model": "local-hash-eval-v1",
        "embedding_dimension": 128,
        "llm_review": "disabled",
        "thresholds": list(args.thresholds),
        "dataset_sha256": {
            name: hashlib.sha256((DATA / name).read_bytes()).hexdigest()
            for name in ("demo_feedback.csv", "golden_dataset.csv")
            if (DATA / name).exists()
        },
        "labels_status": "NOT human-reviewed; initial reference set",
        "per_threshold": {},
    }

    with (
        tempfile.TemporaryDirectory(prefix="insightflow-cluster-eval-") as temp_dir,
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
            project = make_project(db, "Phase 2 Eval Project", "AI Assistant")
            rows = load_demo_rows()
            inserted = import_feedback(db, project.id, rows)
            print(
                f"Project {project.id} created; imported {inserted} feedback items."
            )

            # 1. Build a successful classification input. Either run the real
            # LLM or seed synthetic analyses.
            if args.mode == "full":
                t0 = time.monotonic()
                try:
                    class_run = start_run(db, project.id)
                except ClassificationConfigError as exc:
                    print(f"FAILED: {exc}")
                    return 2
                elapsed_class = time.monotonic() - t0
                report["classification_run"] = {
                    "id": str(class_run.id),
                    "status": class_run.status,
                    "elapsed_seconds": round(elapsed_class, 2),
                    "input_count": class_run.input_count,
                    "success_count": class_run.success_count,
                    "failed_count": class_run.failed_count,
                    "input_tokens": class_run.input_tokens,
                    "output_tokens": class_run.output_tokens,
                    "error_message": class_run.error_message,
                }
                print(
                    f"Phase 1 classification: {class_run.status} in {elapsed_class:.2f}s "
                    f"({class_run.success_count}/{class_run.input_count} succeeded)"
                )
                if class_run.status != "completed":
                    print(
                        "ERROR: classification did not complete; aborting cluster eval.",
                        file=sys.stderr,
                    )
                    return 1
            else:
                full_golden = load_golden_full()
                class_run = _seed_synthetic_analyses(db, project.id, rows, full_golden)
                print(
                    f"Phase 1 seeded synthetically: {class_run.success_count} analyses "
                    f"(intent/pain taken from GT labels)"
                )

            # 2. Configure the embedding + cluster pipeline for the sweep.
            _force_local_embedder()
            db_config.settings.cluster_linkage = args.linkage
            _disable_llm_review()

            # 3. Sweep thresholds. Each run creates a fresh AnalysisRun + new
            # Cluster / ClusterMember rows. Embeddings are idempotent so the
            # second threshold onwards does not re-embed.
            cluster_runs: list[dict] = []
            for threshold in args.thresholds:
                db_config.settings.cluster_distance_threshold = float(threshold)
                t0 = time.monotonic()
                try:
                    cluster_run = start_cluster_run(db, project.id)
                except ClusteringConfigError as exc:
                    print(f"FAILED at threshold={threshold}: {exc}")
                    return 2
                elapsed = time.monotonic() - t0

                predicted = _load_predicted_clusters(db, cluster_run.id)
                metrics = compute_metrics(predicted, golden)
                metrics["run"] = {
                    "id": str(cluster_run.id),
                    "status": cluster_run.status,
                    "input_count": cluster_run.input_count,
                    "success_count": cluster_run.success_count,
                    "elapsed_seconds": round(elapsed, 2),
                    "distance_threshold": cluster_run.distance_threshold
                    if hasattr(cluster_run, "distance_threshold")
                    else float(threshold),
                }
                report["per_threshold"][str(threshold)] = metrics
                cluster_runs.append(
                    {
                        "threshold": threshold,
                        "run_id": str(cluster_run.id),
                        "elapsed": elapsed,
                    }
                )
                print(
                    f"threshold={threshold:.2f}  status={cluster_run.status}  "
                    f"elapsed={elapsed:.2f}s  "
                    f"#predicted={metrics['n_predicted_clusters']}  "
                    f"purity_μ={_format_pct(metrics['purity_micro'])}  "
                    f"coverage={_format_pct(metrics['coverage'])}  "
                    f"fragment={_format_float(metrics['fragmentation_mean'])}  "
                    f"over_merge={_format_pct(metrics['over_merge_ratio'])}"
                )

            _print_table(report["per_threshold"])
            _print_per_threshold_detail(report["per_threshold"])

            # Pick the recommended threshold: highest purity_micro, ties
            # broken by coverage, then by lowest fragmentation.
            def _score(m: dict) -> tuple:
                return (
                    m["purity_micro"] or 0.0,
                    m["coverage"] or 0.0,
                    -(m["fragmentation_mean"] or 0.0),
                )

            best_threshold_key = max(
                report["per_threshold"].keys(),
                key=lambda k: _score(report["per_threshold"][k]),
            )
            best_threshold = float(best_threshold_key)
            report["recommended_threshold"] = {
                "value": best_threshold,
                "rationale": (
                    "highest purity_micro; ties → coverage; then lowest fragmentation"
                ),
                "metrics": report["per_threshold"][best_threshold_key],
            }
            print(f"Recommended threshold: {best_threshold}")
            best_metrics = report["per_threshold"][best_threshold_key]
            print(f"  purity_micro = {best_metrics['purity_micro']:.1%}")
            print(f"  coverage     = {best_metrics['coverage']:.1%}")
            print(f"  fragmentation = {best_metrics['fragmentation_mean']:.2f}")
            print(f"  over_merge   = {best_metrics['over_merge_ratio']:.1%}")
            print("")

            out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-cluster.json"
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            print(f"Detailed report saved to: {out_path.relative_to(ROOT.parent)}")

            for run in cluster_runs:
                if run["threshold"] != best_threshold:
                    continue
                # Sanity: the recommended run must be completed.
                if report["per_threshold"][str(run["threshold"])]["run"]["status"] != "completed":
                    return 1

        server.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
