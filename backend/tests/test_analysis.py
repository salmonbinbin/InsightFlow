"""Phase 1 — AI Feedback Classification tests.

The LLM client is replaced with a stub whose behaviour the test controls; no
real model calls are made. The original 15 import / pagination / health tests
remain in ``test_api.py``.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Feedback as FeedbackModel
from app.services import classification
from app.services.llm_client import LLMResult, LLMUsage

HEADER = ["feedback_id", "content", "source", "user_id", "user_type", "created_at"]


def csv_data(rows: list[list[str]]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


@pytest.fixture
def client(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.session import get_db
    from app.models import Base

    engine = create_engine(os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/test.db"))
    Base.metadata.create_all(engine)

    def override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c, engine
    app.dependency_overrides.clear()
    engine.dispose()


def project(client: TestClient) -> str:
    response = client.post("/projects", json={"name": "测试项目", "product_name": "AI Chat"})
    assert response.status_code == 201
    return response.json()["id"]


def upload(client: TestClient, pid: str, rows: list[list[str]]) -> None:
    resp = client.post(
        f"/projects/{pid}/feedback/import",
        files={"file": ("test.csv", csv_data(rows), "text/csv")},
    )
    assert resp.status_code == 200, resp.text


def _configure_env(monkeypatch) -> None:
    """Patch the Settings singleton so Phase 1 stubs see configured LLM env."""
    from app.db import config as db_config

    monkeypatch.setattr(db_config.settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        db_config.settings, "llm_base_url", "https://example.invalid/anthropic", raising=False
    )
    monkeypatch.setattr(db_config.settings, "llm_model", "stub-model", raising=False)
    monkeypatch.setattr(db_config.settings, "llm_timeout_seconds", 10.0, raising=False)
    monkeypatch.setattr(db_config.settings, "classification_batch_size", 20, raising=False)
    monkeypatch.setattr(db_config.settings, "classification_max_retries", 1, raising=False)


@dataclass
class StubClient:
    """In-process LLM stub.

    ``plan`` is keyed by ``(external_id,)`` and yields the result dict to return
    for that feedback. ``fail_first`` will raise on the first call so we can
    exercise the retry path. ``calls`` records every invocation for assertions.
    """

    plan: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_first: int = 0
    delay_seconds: float = 0.0
    calls: int = 0
    input_tokens_per_call: int = 1
    output_tokens_per_call: int = 1

    def classify_batch(self, items, max_tokens: int = 4096) -> LLMResult:
        self.calls += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            from app.services.llm_client import LLMTransportError

            raise LLMTransportError("simulated transient failure")
        results = []
        for item in items:
            fid = item["feedback_id"]
            payload = self.plan[fid]
            row = {
                "feedback_id": fid,
                "feedback_type": payload["feedback_type"],
                "module": payload["module"],
                "intent": payload["intent"],
                "pain_point": payload.get("pain_point", "stub pain point"),
                "sentiment": payload["sentiment"],
                "severity": payload["severity"],
                "confidence": payload["confidence"],
            }
            results.append(row)
        return LLMResult(payload={"results": results}, usage=LLMUsage(1, 1))

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _install_stub(monkeypatch, stub: StubClient) -> None:
    def factory(model: str | None = None):
        return stub

    monkeypatch.setattr(classification, "build_llm_client", factory)


def _valid_row(external_id: str) -> dict[str, Any]:
    return {
        "feedback_id": external_id,
        "feedback_type": "feature_request",
        "module": "image_upload",
        "intent": "batch_image_upload",
        "pain_point": "Uploading many images requires repeated file selection.",
        "sentiment": "negative",
        "severity": 4,
        "confidence": 0.9,
    }


def test_happy_path_records_seven_fields_and_keeps_raw_intact(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    rows = [
        ["F001", "为什么一次只能上传一张照片？", "support", "U1", "free", ""],
        ["F002", "希望支持语音输入", "support", "U2", "pro", ""],
    ]
    upload(c, pid, rows)
    stub = StubClient(plan={f"F00{i+1}": _valid_row(f"F00{i+1}") for i in range(2)})
    _install_stub(monkeypatch, stub)

    run_resp = c.post(f"/projects/{pid}/analysis")
    assert run_resp.status_code == 200, run_resp.text
    run = run_resp.json()
    assert run["status"] == "completed"
    assert run["input_count"] == 2
    assert run["success_count"] == 2
    assert run["failed_count"] == 0
    assert run["prompt_version"].startswith("feedback-classification")
    assert run["model"] == "stub-model"

    results = c.get(f"/analysis/{run['id']}/results").json()
    assert results["total"] == 2
    fields_seen = {
        (item["feedback_type"], item["module"], item["intent"],
         item["pain_point"], item["sentiment"], item["severity"], item["confidence"])
        for item in results["items"]
    }
    expected_fields = (
        "feature_request", "image_upload", "batch_image_upload",
        "Uploading many images requires repeated file selection.",
        "negative", 4, 0.9,
    )
    assert fields_seen == {expected_fields, expected_fields}
    assert len({item["feedback_id"] for item in results["items"]}) == 2

    raw = c.get(f"/projects/{pid}/feedback").json()
    raw_contents = sorted(item["content"] for item in raw["items"])
    assert raw_contents == sorted(
        ["为什么一次只能上传一张照片？", "希望支持语音输入"]
    )


def test_invalid_enum_does_not_persist(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    rows = [["F1", "原始反馈", "", "", "", ""]]
    upload(c, pid, rows)

    bad_row = _valid_row("F1")
    bad_row["feedback_type"] = "unsupported_type"
    stub = StubClient(plan={"F1": bad_row})
    _install_stub(monkeypatch, stub)

    resp = c.post(f"/projects/{pid}/analysis")
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] in {"partial_failed", "failed"}
    assert run["success_count"] == 0
    assert run["failed_count"] == 1

    results = c.get(f"/analysis/{run['id']}/results").json()
    assert results["total"] == 0

    raw = c.get(f"/projects/{pid}/feedback").json()
    assert raw["total"] == 1
    assert raw["items"][0]["content"] == "原始反馈"


def test_missing_field_rejected(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""]])

    class MissingFieldClient(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            row = _valid_row("F1")
            del row["intent"]
            return LLMResult(
                payload={"results": [row]}, usage=LLMUsage(1, 1)
            )

    stub = MissingFieldClient()
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] in {"partial_failed", "failed"}
    assert run["success_count"] == 0


def test_unknown_id_rejected(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""]])

    class UnknownClient(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            return LLMResult(
                payload={"results": [_valid_row("F1"), _valid_row("F999")]},
                usage=LLMUsage(1, 1),
            )

    stub = UnknownClient()
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] in {"partial_failed", "failed"}
    assert run["failed_count"] == 1
    assert run["success_count"] == 0


def test_duplicate_id_rejected(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""], ["F2", "Y", "", "", "", ""]])

    class DupClient(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            return LLMResult(
                payload={
                    "results": [
                        _valid_row("F1"),
                        _valid_row("F1"),
                    ]
                },
                usage=LLMUsage(1, 1),
            )

    stub = DupClient()
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] in {"partial_failed", "failed"}
    assert run["success_count"] == 0


def test_missing_id_rejected(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""], ["F2", "Y", "", "", "", ""]])

    class MissingClient(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            return LLMResult(
                payload={"results": [_valid_row("F1")]}, usage=LLMUsage(1, 1)
            )

    stub = MissingClient()
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] in {"partial_failed", "failed"}
    assert run["success_count"] == 0


def test_severity_out_of_range_rejected(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""]])
    bad = _valid_row("F1")
    bad["severity"] = 9
    stub = StubClient(plan={"F1": bad})
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] in {"partial_failed", "failed"}
    assert run["failed_count"] == 1


def test_confidence_out_of_range_rejected(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""]])
    bad = _valid_row("F1")
    bad["confidence"] = 1.5
    stub = StubClient(plan={"F1": bad})
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] in {"partial_failed", "failed"}


def test_transient_failure_retried_then_marked_failed(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    monkeypatch.setenv("CLASSIFICATION_MAX_RETRIES", "1")
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""]])
    stub = StubClient(plan={"F1": _valid_row("F1")}, fail_first=10)
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] in {"partial_failed", "failed"}
    assert run["failed_count"] == 1
    assert run["retry_count"] >= 1
    assert stub.calls >= 2


def test_retry_only_failed_and_is_idempotent(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    # Force 2-per-batch so F1+F2 form one batch and F3+F4 form another.
    from app.db import config as db_config

    monkeypatch.setattr(db_config.settings, "classification_batch_size", 2, raising=False)
    pid = project(c)
    upload(
        c,
        pid,
        [
            ["F1", "ok", "", "", "", ""],
            ["F2", "ok", "", "", "", ""],
            ["F3", "fail-batch", "", "", "", ""],
            ["F4", "fail-batch", "", "", "", ""],
        ],
    )

    # First call: F1+F2 succeed, F3+F4 raise transport errors.
    class SplitClient(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            fids = [item["feedback_id"] for item in items]
            if any(fid in {"F3", "F4"} for fid in fids):
                from app.services.llm_client import LLMTransportError

                raise LLMTransportError("simulated transport failure on F3/F4 batch")
            return LLMResult(
                payload={
                    "results": [_valid_row(item["feedback_id"]) for item in items]
                },
                usage=LLMUsage(1, 1),
            )

    stub = SplitClient()
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] == "partial_failed"
    assert run["success_count"] == 2
    assert run["failed_count"] == 2

    # Retry: now stub returns success for the previously failed slice.
    stub2 = StubClient(
        plan={fid: _valid_row(fid) for fid in ("F3", "F4")}
    )
    _install_stub(monkeypatch, stub2)
    retried = c.post(f"/analysis/{run['id']}/retry").json()
    assert retried["status"] == "completed"
    assert retried["success_count"] == 4
    assert retried["failed_count"] == 0
    assert retried["input_count"] == 4

    # Idempotent: F1/F2 must not be re-inserted, F3/F4 must be added exactly once.
    from sqlalchemy.orm import Session

    from app.models import AnalysisRunFailure, FeedbackAnalysis

    with Session(engine) as db:
        analyses = db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == UUID(run["id"]))
        ).all()
        assert len(analyses) == 4
        ids_by_external = {
            db.get(FeedbackModel, UUID(str(a.feedback_id))).external_id: a.id
            for a in analyses
        }
        assert set(ids_by_external) == {"F1", "F2", "F3", "F4"}
        assert len(set(ids_by_external.values())) == 4
        failures = db.scalars(
            select(AnalysisRunFailure).where(AnalysisRunFailure.run_id == UUID(run["id"]))
        ).all()
        assert failures == []


def test_unique_constraint_via_direct_insert(client):
    """UNIQUE(feedback_id, run_id) is enforced by the schema."""
    c, engine = client
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""]])


    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    from app.models import AnalysisRun, Feedback, FeedbackAnalysis, RunStatus

    with Session(engine) as db:
        feedback = db.scalar(select(Feedback).where(Feedback.project_id == UUID(pid)))
        run = AnalysisRun(
            project_id=UUID(pid),
            run_type="classification",
            model="stub",
            prompt_version="v1",
            status=RunStatus.RUNNING.value,
            input_count=1,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.flush()
        db.add(
            FeedbackAnalysis(
                feedback_id=feedback.id,
                run_id=run.id,
                feedback_type="bug",
                module="x",
                intent="x",
                pain_point="x",
                sentiment="neutral",
                severity=1,
                confidence=0.5,
            )
        )
        db.commit()
        # Direct duplicate insert should fail.
        db.add(
            FeedbackAnalysis(
                feedback_id=feedback.id,
                run_id=run.id,
                feedback_type="bug",
                module="x",
                intent="x",
                pain_point="x",
                sentiment="neutral",
                severity=1,
                confidence=0.5,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()


def test_raw_feedback_unchanged_after_classification(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    original = [
        ["F1", "原始中文 \n 多行内容 ", "", "", "", ""],
        ["F2", "tab\there", "", "", "", ""],
    ]
    upload(c, pid, original)
    snapshot_before = c.get(f"/projects/{pid}/feedback").json()

    stub = StubClient(plan={"F1": _valid_row("F1"), "F2": _valid_row("F2")})
    _install_stub(monkeypatch, stub)
    c.post(f"/projects/{pid}/analysis")

    snapshot_after = c.get(f"/projects/{pid}/feedback").json()
    assert snapshot_before == snapshot_after


def test_transaction_rollback_on_partial_persist_failure(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "ok", "", "", "", ""], ["F2", "ok", "", "", "", ""]])

    from app.services import classification as cls_mod
    from app.services.llm_client import LLMResult, LLMUsage

    class HalfBroken(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            # Return two valid rows; the persistence layer will simulate a
            # IntegrityError via _persist_successes by returning mismatched
            # counts.
            return LLMResult(
                payload={"results": [_valid_row("F1"), _valid_row("F2")]},
                usage=LLMUsage(1, 1),
            )

    stub = HalfBroken()
    _install_stub(monkeypatch, stub)
    original = cls_mod._persist_successes

    def broken_persist(db, run, lookup, successes):
        # Only persist the first row to simulate a mid-batch failure.
        return original(db, run, lookup, successes[:1])

    monkeypatch.setattr(cls_mod, "_persist_successes", broken_persist)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] == "failed"
    # Nothing should have been committed.
    from sqlalchemy.orm import Session

    from app.models import FeedbackAnalysis

    with Session(engine) as db:
        rows = db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == UUID(run["id"]))
        ).all()
        assert rows == []
    raw = c.get(f"/projects/{pid}/feedback").json()
    assert raw["total"] == 2


def test_project_isolation(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    p1 = project(c)
    p2 = project(c)
    upload(c, p1, [["F1", "p1", "", "", "", ""]])
    upload(c, p2, [["F1", "p2", "", "", "", ""]])
    stub = StubClient(plan={"F1": _valid_row("F1")})
    _install_stub(monkeypatch, stub)
    run1 = c.post(f"/projects/{p1}/analysis").json()
    run2 = c.post(f"/projects/{p2}/analysis").json()
    assert run1["project_id"] != run2["project_id"]
    items = c.get(f"/analysis/{run1['id']}/results").json()["items"]
    assert len(items) == 1
    # Result must reference p1's feedback, not p2's.
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        feedback = db.get(FeedbackModel, UUID(items[0]["feedback_id"]))
        assert feedback.project_id == UUID(p1)


def test_config_missing_blocks_run(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    pid = project(c)
    upload(c, pid, [["F1", "X", "", "", "", ""]])
    resp = c.post(f"/projects/{pid}/analysis")
    assert resp.status_code == 503
    assert "未配置" in resp.json()["detail"]


def test_missing_project_returns_404(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    from uuid import uuid4

    resp = c.post(f"/projects/{uuid4()}/analysis")
    assert resp.status_code == 404


def test_run_status_endpoint_validation(client):
    c, _ = client
    from uuid import uuid4

    assert c.get(f"/analysis/{uuid4()}").status_code == 404
    assert c.get("/analysis/not-a-uuid").status_code == 422