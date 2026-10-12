"""Phase 2 — Real end-to-end clustering demo via the HTTP API.

Boots an isolated temporary PostgreSQL via ``pgserver``, runs the alembic
migration, then drives the live FastAPI app through the user-facing
endpoints: create a project, import ``data/demo_feedback.csv``, trigger
Phase 1 classification, trigger Phase 2 clustering, and read back the
resulting clusters + members + reviews.

The script prints a human-readable report to stdout and writes a JSON
copy to ``work/eval-results/<timestamp>-e2e.json``. It is the closest
thing to a real user run we can do without an external LLM; the LLM
calls hit the configured endpoint (or a stub if no key is set — see
``--stub-llm``).

Usage::

    # With real LLM:
    LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=... \\
    uv run --with pgserver==0.1.4 python scripts/run_clustering_e2e.py

    # Without any LLM credentials — uses deterministic stubs:
    uv run --with pgserver==0.1.4 python scripts/run_clustering_e2e.py --stub-llm
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pgserver
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = ROOT.parent / "data"
RESULTS_DIR = ROOT.parent / "work" / "eval-results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Pull Anthropic-style env vars into the LLM_* names the Settings singleton
# recognises, mirroring ``run_classification_eval.py``.
for _src, _dst in (
    ("ANTHROPIC_AUTH_TOKEN", "LLM_API_KEY"),
    ("ANTHROPIC_BASE_URL", "LLM_BASE_URL"),
    ("ANTHROPIC_MODEL", "LLM_MODEL"),
):
    _val = os.environ.get(_src)
    if _val and not os.environ.get(_dst):
        os.environ[_dst] = _val


def _install_stubs() -> None:
    """Replace the LLM clients with deterministic stubs (in-process).

    The patching uses ``setattr`` on the *imported* modules because the
    LLM clients are referenced as module-level names inside
    ``classification`` and ``cluster_runner``. This mirrors the strategy
    :mod:`tests.test_phase2` uses but without the ``pytest`` fixture.
    """
    from app.services import classification, cluster_runner
    from app.services.embedding import LocalHashEmbedder
    from app.services.llm_client import LLMResult, LLMUsage

    class _ClassificationStub:
        calls = 0

        def classify_batch(self, items, max_tokens: int = 4096):
            self.calls += 1
            results = []
            for item in items:
                content = (item.get("content") or "").lower()
                if "上传" in content or "图片" in content:
                    module, intent = "image_upload", "batch_image_upload"
                    feedback_type, sentiment = "feature_request", "negative"
                elif "pdf" in content or "压缩" in content:
                    module, intent = "file_upload", "upload_pdf_reliably"
                    feedback_type, sentiment = "bug", "negative"
                elif "等" in content or "慢" in content or "快" in content:
                    module, intent = "response_speed", "faster_response"
                    feedback_type, sentiment = "performance_issue", "negative"
                elif "搜索" in content or "结果" in content:
                    module, intent = "search_accuracy", "relevant_search_results"
                    feedback_type, sentiment = "content_issue", "negative"
                elif "记得" in content or "偏好" in content:
                    module, intent = "memory", "remember_preferences"
                    feedback_type, sentiment = "feature_request", "negative"
                elif "忘" in content or "上下文" in content:
                    module, intent = "context_loss", "retain_conversation_context"
                    feedback_type, sentiment = "content_issue", "negative"
                elif "导出" in content or "markdown" in content.lower():
                    module, intent = "export_sharing", "export_markdown"
                    feedback_type, sentiment = "feature_request", "neutral"
                elif "清楚" in content or "总结" in content:
                    module, intent = "response_quality", "clear_summary"
                    feedback_type, sentiment = "positive_feedback", "positive"
                else:
                    module, intent = "other", "unknown"
                    feedback_type, sentiment = "other", "neutral"
                results.append(
                    {
                        "feedback_id": item["feedback_id"],
                        "feedback_type": feedback_type,
                        "module": module,
                        "intent": intent,
                        "pain_point": f"pain for {item['feedback_id']}",
                        "sentiment": sentiment,
                        "severity": 3,
                        "confidence": 0.85,
                    }
                )
            return LLMResult(payload={"results": results}, usage=LLMUsage(1, 1))

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    class _ReviewStub:
        """Default-action review stub: keep everything, with deterministic names."""

        def __init__(self):
            self.calls = 0

        def review_clusters(self, items):
            self.calls += 1
            reviews = []
            for index, item in enumerate(items):
                member_ids = [m["feedback_id"] for m in item.members]
                reviews.append(
                    {
                        "cluster_index": index,
                        "action": "keep",
                        "reason": f"stub review for cluster {index}",
                        "name": f"topic_{index}",
                        "summary": f"auto-summary for {len(member_ids)} members",
                        "confidence": 0.8,
                        "groups": [{"name": f"topic_{index}", "feedback_ids": member_ids}],
                        "outlier_ids": [],
                    }
                )
            return LLMResult(payload={"reviews": reviews}, usage=LLMUsage(1, 1))

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    setattr(classification, "build_llm_client", lambda model=None: _ClassificationStub())
    setattr(cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-e2e-v1", 128))
    setattr(cluster_runner, "ClusterReviewClient", _ReviewStub)


def _print_section(title: str) -> None:
    bar = "=" * 78
    print("")
    print(bar)
    print(title)
    print(bar)


def _print_clusters(clusters_payload: dict) -> None:
    items = clusters_payload.get("items", [])
    if not items:
        print("(no clusters returned)")
        return
    for cl in items:
        # Inline the essentials: name, source, counts, threshold, parent.
        print(
            f"  • {cl.get('name') or '(unnamed)':<24}  "
            f"source={cl['source']:<11}  "
            f"feedback={cl['feedback_count']:>3}  "
            f"users={cl['unique_user_count']:>2}  "
            f"avg_sev={cl['average_severity']}  "
            f"neg={cl['negative_ratio']}  "
            f"threshold={cl['distance_threshold']:.2f}  "
            f"parent={cl.get('parent_cluster_id')}"
        )
        if cl.get("summary"):
            summary = cl["summary"]
            if len(summary) > 200:
                summary = summary[:197] + "..."
            print(f"      summary: {summary}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stub-llm",
        action="store_true",
        help="replace LLM clients with deterministic stubs (no API key required)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="cluster distance threshold to use (default 0.45)",
    )
    args = parser.parse_args(argv)

    if not args.stub_llm:
        if not (os.environ.get("LLM_API_KEY") and os.environ.get("LLM_BASE_URL")):
            print(
                "ERROR: real LLM mode requires LLM_API_KEY + LLM_BASE_URL "
                "(and ideally LLM_MODEL). Pass --stub-llm to use deterministic stubs.",
                file=sys.stderr,
            )
            return 2

    # We import app code lazily *after* setting the DATABASE_URL env so the
    # Settings singleton (which reads DATABASE_URL eagerly) points at the
    # temp pgserver from the first import.
    from app.db import config as db_config  # noqa: WPS433
    from app.main import app  # noqa: WPS433

    if args.stub_llm:
        _install_stubs()
        print("LLM mode: deterministic stubs (no external calls)")
    else:
        print(
            "LLM mode: real "
            f"(model={os.environ.get('LLM_MODEL')!r}, "
            f"base={os.environ.get('LLM_BASE_URL')!r})"
        )
    print(f"Cluster threshold: {args.threshold}")
    print("")

    with (
        tempfile.TemporaryDirectory(prefix="insightflow-e2e-") as temp_dir,
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

        # Override the FastAPI get_db dependency to talk to our temp server.
        from app.db.session import get_db  # noqa: WPS433

        def override():
            with Session_() as db:
                yield db

        app.dependency_overrides[get_db] = override

        # Always use the local embedder so the run is deterministic; the
        # LLM call paths (classification + review) follow the stub / real
        # configuration set above.
        from app.services import cluster_runner  # noqa: WPS433
        from app.services.embedding import LocalHashEmbedder  # noqa: WPS433

        original_build_embedder = cluster_runner.build_embedder
        cluster_runner.build_embedder = lambda: LocalHashEmbedder(
            "local-hash-e2e-v1", 128
        )

        report: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stub_llm": args.stub_llm,
            "threshold": args.threshold,
            "embedding_model": "local-hash-e2e-v1",
            "embedding_dimension": 128,
        }

        try:
            with TestClient(app) as c:
                # 1. Create project.
                _print_section("1. Create project")
                resp = c.post(
                    "/projects",
                    json={"name": "Phase 2 E2E Demo", "product_name": "AI Chat"},
                )
                if resp.status_code != 201:
                    print(f"FAIL: {resp.status_code} {resp.text}")
                    return 1
                project = resp.json()
                pid = project["id"]
                print(f"Created project {pid}")

                # 2. Import demo feedback.
                _print_section("2. Import demo feedback")
                with (DATA / "demo_feedback.csv").open(encoding="utf-8-sig") as fp:
                    body = fp.read().encode("utf-8-sig")
                resp = c.post(
                    f"/projects/{pid}/feedback/import",
                    files={"file": ("demo_feedback.csv", body, "text/csv")},
                )
                if resp.status_code != 200:
                    print(f"FAIL: {resp.status_code} {resp.text}")
                    return 1
                imported = resp.json()
                print(f"Imported {imported.get('inserted', '?')} feedback items")

                # 3. Run Phase 1 classification.
                _print_section("3. Run Phase 1 classification (HTTP)")
                t0 = time.monotonic()
                resp = c.post(f"/projects/{pid}/analysis")
                elapsed_class = time.monotonic() - t0
                if resp.status_code != 200:
                    print(f"FAIL: {resp.status_code} {resp.text}")
                    return 1
                class_run = resp.json()
                print(
                    f"Classification: status={class_run['status']}  "
                    f"input={class_run['input_count']}  "
                    f"success={class_run['success_count']}  "
                    f"failed={class_run['failed_count']}  "
                    f"retries={class_run['retry_count']}  "
                    f"elapsed={elapsed_class:.2f}s"
                )
                if class_run.get("error_message"):
                    print(f"  error_message: {class_run['error_message']}")
                report["classification"] = {
                    "id": class_run["id"],
                    "status": class_run["status"],
                    "input_count": class_run["input_count"],
                    "success_count": class_run["success_count"],
                    "failed_count": class_run["failed_count"],
                    "retry_count": class_run["retry_count"],
                    "input_tokens": class_run["input_tokens"],
                    "output_tokens": class_run["output_tokens"],
                    "latency_ms": class_run["latency_ms"],
                    "elapsed_seconds": round(elapsed_class, 3),
                    "error_message": class_run.get("error_message"),
                }

                if class_run["status"] != "completed":
                    print("ERROR: classification did not complete; aborting")
                    return 1

                # 4. Run Phase 2 clustering.
                _print_section("4. Run Phase 2 clustering (HTTP)")
                db_config.settings.cluster_distance_threshold = args.threshold
                t0 = time.monotonic()
                resp = c.post(f"/projects/{pid}/clustering")
                elapsed_cluster = time.monotonic() - t0
                if resp.status_code != 200:
                    print(f"FAIL: {resp.status_code} {resp.text}")
                    return 1
                cluster_run = resp.json()
                print(
                    f"Clustering:    status={cluster_run['status']}  "
                    f"input={cluster_run['input_count']}  "
                    f"success={cluster_run['success_count']}  "
                    f"attempts={cluster_run['attempt_count']}  "
                    f"elapsed={elapsed_cluster:.2f}s"
                )
                if cluster_run.get("error_message"):
                    print(f"  error_message: {cluster_run['error_message']}")
                report["clustering"] = {
                    "id": cluster_run["id"],
                    "status": cluster_run["status"],
                    "input_count": cluster_run["input_count"],
                    "success_count": cluster_run["success_count"],
                    "attempt_count": cluster_run["attempt_count"],
                    "input_tokens": cluster_run["input_tokens"],
                    "output_tokens": cluster_run["output_tokens"],
                    "latency_ms": cluster_run["latency_ms"],
                    "elapsed_seconds": round(elapsed_cluster, 3),
                    "error_message": cluster_run.get("error_message"),
                }

                # 5. List clusters and members.
                _print_section("5. List clusters (latest run)")
                resp = c.get(f"/projects/{pid}/clusters")
                if resp.status_code != 200:
                    print(f"FAIL: {resp.status_code} {resp.text}")
                    return 1
                listing = resp.json()
                print(f"Total clusters: {listing['total']}")
                _print_clusters(listing)
                report["cluster_listing"] = {
                    "total": listing["total"],
                    "items": [
                        {
                            "id": cl["id"],
                            "name": cl.get("name"),
                            "source": cl["source"],
                            "feedback_count": cl["feedback_count"],
                            "unique_user_count": cl["unique_user_count"],
                            "average_severity": cl["average_severity"],
                            "negative_ratio": cl["negative_ratio"],
                            "distance_threshold": cl["distance_threshold"],
                            "parent_cluster_id": cl.get("parent_cluster_id"),
                            "summary": cl.get("summary"),
                        }
                        for cl in listing["items"]
                    ],
                }

                # 6. For each cluster, list its members.
                _print_section("6. Cluster members")
                members_payload: list[dict] = []
                for cl in listing["items"]:
                    resp = c.get(f"/clusters/{cl['id']}/members")
                    if resp.status_code != 200:
                        print(f"FAIL listing members: {resp.status_code} {resp.text}")
                        return 1
                    m = resp.json()
                    members_payload.append(
                        {
                            "cluster_id": cl["id"],
                            "cluster_name": cl.get("name"),
                            "cluster_source": cl["source"],
                            "total": m["total"],
                            "members": m["items"],
                        }
                    )
                    label = cl.get("name") or cl["id"][:8]
                    member_ids = [it["feedback_analysis_id"] for it in m["items"]]
                    print(
                        f"  • {label:<24}  members={m['total']:>2}  "
                        f"analysis_ids={member_ids[:6]}{'…' if len(member_ids) > 6 else ''}"
                    )
                report["members_by_cluster"] = members_payload

                # 7. Cluster reviews (LLM review audit).
                _print_section("7. Cluster reviews (LLM audit)")
                review_payload: list[dict] = []
                for cl in listing["items"]:
                    if cl["source"] == "algorithmic":
                        continue  # no review row for un-reviewed clusters
                    resp = c.get(f"/clusters/{cl['id']}/review")
                    if resp.status_code != 200:
                        print(f"FAIL: {resp.status_code} {resp.text}")
                        return 1
                    r = resp.json()
                    if r is None:
                        continue
                    review_payload.append(
                        {
                            "cluster_id": cl["id"],
                            "cluster_name": cl.get("name"),
                            "action": r["action"],
                            "prompt_version": r["prompt_version"],
                            "reason": r["reason"],
                        }
                    )
                    label = cl.get("name") or cl["id"][:8]
                    reason = (r.get("reason") or "")[:80]
                    print(f"  • {label:<24}  action={r['action']:<6}  reason={reason!r}")
                if not review_payload:
                    print("(no LLM reviews — review step was disabled or all algorithmic)")
                report["reviews"] = review_payload

                # 8. Run status endpoints.
                _print_section("8. Status endpoints")
                resp = c.get(f"/analysis/{class_run['id']}")
                print(f"GET /analysis/{class_run['id']}: {resp.status_code} (status={resp.json().get('status')})")
                resp = c.get(f"/clustering/{cluster_run['id']}")
                print(f"GET /clustering/{cluster_run['id']}: {resp.status_code} (status={resp.json().get('status')})")
                resp = c.get("/health")
                print(f"GET /health: {resp.status_code} {resp.text.strip()}")
                resp = c.get("/health/ready")
                print(f"GET /health/ready: {resp.status_code} {resp.text.strip()}")

        finally:
            cluster_runner.build_embedder = original_build_embedder
            app.dependency_overrides.clear()

        out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-e2e.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print("")
        print(f"Detailed report saved to: {out_path.relative_to(ROOT.parent)}")

        server.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
