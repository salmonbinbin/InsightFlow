"""独立验收：断言规范要求，不将已知缺陷标为 xfail。全部数据使用测试库。"""

import copy
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import httpx
import pytest
import test_analysis
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_analysis import (
    StubClient,
    _configure_env,
    _install_stub,
    _valid_row,
    project,
    upload,
)

from app.db.config import settings
from app.models import AnalysisRun, AnalysisRunFailure, FeedbackAnalysis
from app.services import analysis_runner, classification
from app.services.llm_client import (
    LLMClient,
    LLMProtocolError,
    LLMResult,
    LLMTransportError,
    LLMUsage,
    _parse_messages_response,
)

client = test_analysis.client


@pytest.mark.parametrize(
    "field,value",
    [
        ("severity", True),
        ("severity", "3"),
        ("severity", 0),
        ("severity", 6),
        ("confidence", True),
        ("confidence", "0.5"),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("module", ""),
        ("module", " "),
        ("module", "a" * 101),
        ("intent", ""),
        ("intent", "a" * 201),
        ("pain_point", "a" * 4001),
        ("pain_point", 42),
        ("pain_point", []),
        ("pain_point", {}),
        ("feedback_type", "invalid"),
        ("feedback_type", []),
        ("sentiment", {}),
    ],
)
def test_schema_rejects_invalid_types(field, value):
    row = _valid_row("F1")
    row[field] = value
    with pytest.raises(classification.ClassificationValidationError):
        classification.validate_batch_output({"results": [row]}, ["F1"])


@pytest.mark.parametrize("field", list(_valid_row("F1")))
def test_every_required_field_is_required(field):
    row = _valid_row("F1")
    del row[field]
    with pytest.raises(classification.ClassificationValidationError):
        classification.validate_batch_output({"results": [row]}, ["F1"])


@pytest.mark.parametrize("ids", [[], ["F1"], ["F1", "F1"], ["F1", "F9"], ["F2", "F1"]])
def test_id_integrity(ids):
    with pytest.raises(classification.ClassificationValidationError):
        classification.validate_batch_output(
            {"results": [_valid_row(i) for i in ids]}, ["F1", "F2"]
        )


@pytest.mark.parametrize("count", [0, 1, 20, 21, 41])
def test_batch_boundaries_and_history(client, monkeypatch, count):
    c, engine = client
    _configure_env(monkeypatch)
    pid = project(c)
    if count:
        upload(
            c,
            pid,
            [
                [f"F{i}", " 原文\n第二行 ", "source", "U1", "pro", "2026-10-01"]
                for i in range(count)
            ],
        )
    before = c.get(f"/projects/{pid}/feedback").json()
    stub = StubClient(plan={f"F{i}": _valid_row(f"F{i}") for i in range(count)})
    _install_stub(monkeypatch, stub)
    first = c.post(f"/projects/{pid}/analysis").json()
    assert first["status"] == "completed"
    assert first["success_count"] == first["processed_count"] == count
    assert first["failed_count"] == 0
    assert stub.calls == (count + 19) // 20
    original = c.get(f"/analysis/{first['id']}/results").json()
    assert c.post(f"/analysis/{first['id']}/retry").status_code == 409
    second = c.post(f"/projects/{pid}/analysis").json()
    assert second["id"] != first["id"]
    assert c.get(f"/analysis/{first['id']}/results").json() == original
    assert c.get(f"/projects/{pid}/feedback").json() == before
    with Session(engine) as db:
        assert db.get(AnalysisRun, UUID(second["id"])).success_count == count


def test_wrong_pain_point_must_not_reach_database(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", " 原文\n ", "", "", "", ""]])
    row = _valid_row("F1")
    row["pain_point"] = {"unexpected": "object"}
    _install_stub(monkeypatch, StubClient(plan={"F1": row}))
    run = c.post(f"/projects/{pid}/analysis").json()
    with Session(engine) as db:
        records = db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == UUID(run["id"]))
        ).all()
        assert records == [], f"invalid output stored as {records[0].pain_point!r}"


def test_schema_failed_attempt_usage_is_counted(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])

    class InvalidThenValid(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            time.sleep(0.025)
            row = _valid_row("F1")
            if self.calls == 1:
                row["severity"] = 99
            return LLMResult({"results": [row]}, LLMUsage(10, 20))

    stub = InvalidThenValid()
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] == "completed" and stub.calls == 2
    assert run["input_tokens"] == 20 and run["output_tokens"] == 40
    assert run["latency_ms"] >= 50


def test_missing_usage_is_unknown():
    parsed = _parse_messages_response(
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "submit_classifications",
                    "input": {"results": [_valid_row("F1")]},
                }
            ]
        }
    )
    assert parsed.usage.input_tokens is None and parsed.usage.output_tokens is None


def test_wrong_tool_is_rejected():
    with pytest.raises(LLMProtocolError):
        _parse_messages_response(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "other_tool",
                        "input": {"results": [_valid_row("F1")]},
                    }
                ]
            }
        )


def test_http_error_does_not_echo_credentials():
    sentinel = "ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL"
    with LLMClient(sentinel, "https://example.invalid", "stub", 1) as llm:
        llm._client.close()
        llm._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(401, text="invalid key " + sentinel)
            )
        )
        with pytest.raises(LLMProtocolError) as exc:
            llm.classify_batch([{"feedback_id": "F1", "content": "x"}])
    assert sentinel not in str(exc.value)


def test_failure_attempts_match_actual_calls(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 2)
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])
    stub = StubClient(fail_first=10)
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert stub.calls == 3 and run["retry_count"] == 2
    with Session(engine) as db:
        failure = db.scalar(
            select(AnalysisRunFailure).where(AnalysisRunFailure.run_id == UUID(run["id"]))
        )
        assert failure.attempts == 3


def test_failed_run_retry_preserves_model_provenance(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(fail_first=1))
    run = c.post(f"/projects/{pid}/analysis").json()
    monkeypatch.setattr(settings, "llm_model", "different-model")
    called_models = []

    def factory(model: str | None = None):
        called_models.append(model if model is not None else settings.llm_model)
        return StubClient(plan={"F1": _valid_row("F1")})

    monkeypatch.setattr(classification, "build_llm_client", factory)
    response = c.post(f"/analysis/{run['id']}/retry")
    assert response.status_code == 409 or called_models == [run["model"]]


def test_interrupted_run_can_recover_without_losing_success(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_batch_size", 1)
    pid = project(c)
    upload(c, pid, [["F1", "原文1", "", "", "", ""], ["F2", "原文2", "", "", "", ""]])

    class InterruptClient(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt("simulated process interruption")
            return LLMResult({"results": [_valid_row(items[0]["feedback_id"])]}, LLMUsage(1, 1))

    _install_stub(monkeypatch, InterruptClient())
    with Session(engine) as db:
        with pytest.raises(KeyboardInterrupt):
            analysis_runner.start_run(db, UUID(pid))
    _install_stub(monkeypatch, StubClient(plan={"F1": _valid_row("F1"), "F2": _valid_row("F2")}))
    with Session(engine) as db:
        run = db.scalar(select(AnalysisRun).where(AnalysisRun.project_id == UUID(pid)))
        retried = analysis_runner.retry_run(db, run.id)
        assert retried.status == "completed"
        assert retried.success_count == 2


def test_persistence_failure_keeps_previous_batch_and_retryability(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_batch_size", 1)
    pid = project(c)
    upload(c, pid, [["F1", "原文1", "", "", "", ""], ["F2", "原文2", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(plan={"F1": _valid_row("F1"), "F2": _valid_row("F2")}))
    original = classification._persist_successes
    calls = 0

    def broken(db, run, lookup, rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            return 0
        return original(db, run, lookup, rows)

    monkeypatch.setattr(classification, "_persist_successes", broken)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["processed_count"] == run["input_count"] == 2
    assert run["success_count"] == 1 and run["failed_count"] == 1
    assert c.get(f"/analysis/{run['id']}/results").json()["total"] == 1


def test_concurrent_retry_is_controlled(client, monkeypatch):
    c, engine = client
    if engine.dialect.name != "postgresql":
        pytest.skip("concurrency contract is tested on PostgreSQL")
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(fail_first=1))
    run = c.post(f"/projects/{pid}/analysis").json()
    _install_stub(monkeypatch, StubClient(plan={"F1": _valid_row("F1")}))
    barrier = Barrier(2)

    def retry():
        with Session(engine) as db:
            db.get(AnalysisRun, UUID(run["id"]))
            # Synchronize after both requests have read the eligible run and failures.
            real_scalars = db.scalars

            def scalars(stmt, *args, **kwargs):
                result = real_scalars(stmt, *args, **kwargs)
                if "FROM analysis_run_failures" in str(stmt):
                    rows = result.all()
                    barrier.wait(timeout=5)

                    class Rows:
                        def all(self):
                            return rows

                    return Rows()
                return result

            db.scalars = scalars
            try:
                return analysis_runner.retry_run(db, UUID(run["id"])).status
            except ValueError:
                return "conflict"
            except Exception as exc:
                return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: retry(), range(2)))
    assert all(s in {"completed", "conflict"} for s in outcomes), outcomes
    with Session(engine) as db:
        current = db.get(AnalysisRun, UUID(run["id"]))
        assert current.success_count == current.processed_count == 1


@pytest.mark.parametrize("exc", [LLMTransportError("timeout"), LLMProtocolError("bad JSON")])
def test_retry_exhaustion_and_raw_immutability(client, monkeypatch, exc):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", " 原文\n末尾 ", "source", "U1", "pro", "2026-10-01"]])
    before = c.get(f"/projects/{pid}/feedback").json()

    class Broken(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            self.calls += 1
            raise copy.copy(exc)

    stub = Broken()
    _install_stub(monkeypatch, stub)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert stub.calls == 2
    assert run["status"] == "failed"
    assert run["failed_count"] == run["processed_count"] == run["input_count"] == 1
    _install_stub(monkeypatch, StubClient(plan={"F1": _valid_row("F1")}))
    retried = c.post(f"/analysis/{run['id']}/retry").json()
    assert retried["status"] == "completed"
    assert c.get(f"/projects/{pid}/feedback").json() == before
