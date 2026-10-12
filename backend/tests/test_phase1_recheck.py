"""2026-10-12 独立复检：修复组合边界，不替代原验收用例。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from uuid import UUID

import pytest
import test_analysis
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_analysis import StubClient, _configure_env, _install_stub, _valid_row, project, upload

from app.db.config import settings
from app.models import AnalysisRun, AnalysisRunFailure, FeedbackAnalysis
from app.services import analysis_runner, classification
from app.services.llm_client import LLMResult, LLMUsage, _parse_messages_response

client = test_analysis.client


def test_retry_does_not_expand_original_input(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    pid = project(c)
    upload(c, pid, [["F1", "原始输入", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(fail_first=5))
    run = c.post(f"/projects/{pid}/analysis").json()
    upload(c, pid, [["F2", "Run创建后新导入", "", "", "", ""]])

    class Capture(StubClient):
        seen = []

        def classify_batch(self, items, max_tokens=4096):
            self.seen.extend(i["feedback_id"] for i in items)
            return LLMResult(
                {"results": [_valid_row(i["feedback_id"]) for i in items]}, LLMUsage(1, 1)
            )

    stub = Capture()
    _install_stub(monkeypatch, stub)
    response = c.post(f"/analysis/{run['id']}/retry").json()
    assert stub.seen == ["F1"], {"actual_ids": stub.seen, "response": response}
    assert (
        response["status"] == "completed"
        and response["input_count"] == response["success_count"] == 1
    )
    with Session(engine) as db:
        assert (
            len(
                db.scalars(
                    select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == UUID(run["id"]))
                ).all()
            )
            == 1
        )


def test_unknown_usage_counts_calls_not_fields(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])

    class Unknown(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            return LLMResult({"results": [_valid_row("F1")]}, LLMUsage(None, None))

    _install_stub(monkeypatch, Unknown())
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["attempt_count"] == 1
    assert run["unknown_token_calls"] == 1, run


def test_wrong_tool_error_redacts_provider_echo(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    sentinel = "FAKE_RECHECK_CREDENTIAL_NEVER_REAL"
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])

    class WrongTool(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            return _parse_messages_response(
                {"content": [{"type": "tool_use", "name": sentinel, "input": {}}]}
            )

    _install_stub(monkeypatch, WrongTool())
    run = c.post(f"/projects/{pid}/analysis").json()
    with Session(engine) as db:
        failure = db.scalar(
            select(AnalysisRunFailure).where(AnalysisRunFailure.run_id == UUID(run["id"]))
        )
        assert sentinel not in (failure.last_error or "")


def test_malformed_usage_is_controlled_protocol_error():
    from app.services.llm_client import LLMProtocolError

    data = {
        "content": [
            {
                "type": "tool_use",
                "name": "submit_classifications",
                "input": {"results": [_valid_row("F1")]},
            }
        ],
        "usage": ["not", "an", "object"],
    }
    try:
        result = _parse_messages_response(data)
    except LLMProtocolError:
        return
    assert result.usage.input_tokens is None and result.usage.output_tokens is None


def test_cached_usage_is_included_in_input_tokens():
    data = {
        "content": [{"type": "tool_use", "name": "submit_classifications", "input": {"results": []}}],
        "usage": {
            "input_tokens": 1,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 128,
            "output_tokens": 20,
        },
    }
    usage = _parse_messages_response(data).usage
    assert usage.input_tokens == 139 and usage.output_tokens == 20
    data["usage"]["cache_read_input_tokens"] = "not a number"
    assert _parse_messages_response(data).usage.input_tokens is None


def test_retry_rejects_prompt_version_drift(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(fail_first=5))
    run = c.post(f"/projects/{pid}/analysis").json()
    with Session(engine) as db:
        stored = db.get(AnalysisRun, UUID(run["id"]))
        stored.prompt_version = "historical-prompt-version"
        db.commit()
    stub = StubClient(plan={"F1": _valid_row("F1")})
    _install_stub(monkeypatch, stub)
    response = c.post(f"/analysis/{run['id']}/retry")
    assert response.status_code == 409 and stub.calls == 0, response.text


def test_multibatch_retry_excludes_concurrent_worker(client, monkeypatch):
    c, engine = client
    if engine.dialect.name != "postgresql":
        pytest.skip("真实PostgreSQL并发复检")
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    monkeypatch.setattr(settings, "classification_batch_size", 1)
    pid = project(c)
    upload(c, pid, [["F1", "原文1", "", "", "", ""], ["F2", "原文2", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(fail_first=10))
    run = c.post(f"/projects/{pid}/analysis").json()
    waiting, release = Event(), Event()
    lock = Lock()

    class Held(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            with lock:
                self.calls += 1
                count = self.calls
            if count == 2:
                waiting.set()
                assert release.wait(10), "test release timeout"
            return LLMResult(
                {"results": [_valid_row(i["feedback_id"]) for i in items]}, LLMUsage(1, 1)
            )

    stub = Held()
    _install_stub(monkeypatch, stub)

    def retry():
        with Session(engine) as db:
            try:
                return analysis_runner.retry_run(db, UUID(run["id"])).status
            except ValueError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(retry)
        assert waiting.wait(5), "first worker did not reach batch 2"
        second = pool.submit(retry)
        try:
            second_result = second.result(timeout=5)
        finally:
            release.set()
        first_result = first.result(timeout=5)
    assert second_result == "conflict", {
        "first": first_result,
        "second": second_result,
        "calls": stub.calls,
    }
    with Session(engine) as db:
        current = db.get(AnalysisRun, UUID(run["id"]))
        assert (
            current.status == "completed" and current.success_count == current.processed_count == 2
        )


def test_persistence_failure_still_accounts_completed_call(client, monkeypatch):
    c, _ = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_batch_size", 1)
    pid = project(c)
    upload(c, pid, [["F1", "原文1", "", "", "", ""], ["F2", "原文2", "", "", "", ""]])
    stub = StubClient(plan={"F1": _valid_row("F1"), "F2": _valid_row("F2")})
    _install_stub(monkeypatch, stub)
    original = classification._persist_successes
    seen = 0

    def broken(db, run, lookup, rows):
        nonlocal seen
        seen += 1
        if seen == 2:
            return 0
        return original(db, run, lookup, rows)

    monkeypatch.setattr(classification, "_persist_successes", broken)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert stub.calls == 2 and run["status"] == "partial_failed"
    assert run["attempt_count"] == 2 and run["input_tokens"] == run["output_tokens"] == 2, run


def test_retry_rejects_database_lock_from_another_connection(client, monkeypatch):
    c, engine = client
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL跨连接执行锁复检")
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(fail_first=2))
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] == "failed"
    key = UUID(run["id"]).int & 0x7FFF_FFFF_FFFF_FFFF
    with engine.connect() as lock_connection:
        assert lock_connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        try:
            response = c.post(f"/analysis/{run['id']}/retry")
            assert response.status_code == 409, response.text
        finally:
            lock_connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def test_untrusted_protocol_error_does_not_persist_arbitrary_secret(client, monkeypatch):
    from app.services.llm_client import LLMProtocolError

    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_max_retries", 0)
    marker = "Q9unusualPrivateValue7z"
    pid = project(c)
    upload(c, pid, [["F1", "原文", "", "", "", ""]])

    class Malicious(StubClient):
        def classify_batch(self, items, max_tokens=4096):
            raise LLMProtocolError("provider diagnostic " + marker)

    _install_stub(monkeypatch, Malicious())
    run = c.post(f"/projects/{pid}/analysis").json()
    with Session(engine) as db:
        failure = db.scalar(
            select(AnalysisRunFailure).where(AnalysisRunFailure.run_id == UUID(run["id"]))
        )
        assert marker not in (failure.last_error or "")
    assert marker not in str(run)


def test_integrity_error_after_model_call_preserves_usage_and_retry(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    monkeypatch.setattr(settings, "classification_batch_size", 1)
    pid = project(c)
    upload(c, pid, [["F1", "原文1", "", "", "", ""], ["F2", "原文2", "", "", "", ""]])
    _install_stub(monkeypatch, StubClient(plan={"F1": _valid_row("F1"), "F2": _valid_row("F2")}))
    original = classification._persist_successes
    seen = 0

    def broken(db, run, lookup, rows):
        nonlocal seen
        seen += 1
        if seen == 2:
            raise IntegrityError("INSERT", {}, Exception("synthetic constraint failure"))
        return original(db, run, lookup, rows)

    monkeypatch.setattr(classification, "_persist_successes", broken)
    run = c.post(f"/projects/{pid}/analysis").json()
    assert run["status"] == "partial_failed" and run["success_count"] == run["failed_count"] == 1
    assert run["processed_count"] == run["input_count"] == run["attempt_count"] == 2
    assert run["input_tokens"] == run["output_tokens"] == 2
    with Session(engine) as db:
        rows = db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == UUID(run["id"]))
        ).all()
        failures = db.scalars(
            select(AnalysisRunFailure).where(AnalysisRunFailure.run_id == UUID(run["id"]))
        ).all()
        assert len(rows) == len(failures) == 1
        assert "synthetic" not in (failures[0].last_error or "")
