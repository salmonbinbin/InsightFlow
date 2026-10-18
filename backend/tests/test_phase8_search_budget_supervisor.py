"""Fallbacks consume the same Run budget and preserve official-body grounding."""

import pytest
from sqlalchemy import select
from test_phase8_pipeline_v7_supervisor import (
    SessionLocal as _SessionLocal,
)
from test_phase8_pipeline_v7_supervisor import (
    engine as _engine,
)
from test_phase8_pipeline_v7_supervisor import (
    fake_settings as _settings,
)
from test_phase8_pipeline_v7_supervisor import (
    install,
    output,
    run_case,
)

from app.models import ResearchSource
from app.services import research_search_adapters as adapters
from app.services import research_service as service
from app.services.research_provider import ResearchProviderResponseError

SessionLocal = _SessionLocal
engine = _engine
fake_settings = _settings


@pytest.mark.parametrize("all_fail", [False, True])
def test_fallback_uses_same_run_budget_and_persists_actual_provider(
    SessionLocal, fake_settings, monkeypatch, all_fail
):
    install(monkeypatch, output)
    fake_settings.research_search_provider = "tavily"
    fake_settings.research_search_fallback = "minimax"
    fake_settings.minimax_search_api_key = "fake-key"
    original = service.ResearchProviderClient
    calls = []

    def build(settings, name, **kwargs):
        client = original()
        search = client.search

        def invoke(q):
            calls.append(name)
            client.last_attempt_count = 1
            client.last_attempts = [{"attempt": 1, "code": "authentication", "retryable": False}]
            if name == "tavily" or all_fail:
                raise ResearchProviderResponseError(
                    "fixed safe error", code="authentication", http_status=401
                )
            from dataclasses import replace

            return replace(search(q), provider="minimax")

        client.search = invoke
        return client

    monkeypatch.setattr(adapters, "build_search_client", build)
    db, run = run_case(SessionLocal)
    try:
        assert calls[:2] == ["tavily", "minimax"]
        assert len(calls) <= 3
        assert run.execution_trace["budgets"]["logical_searches"] == len(calls)
        events = [e for e in run.execution_trace["events"] if e["stage"] == "search"]
        assert events[0]["failure"] == {
            "code": "authentication",
            "retryable": False,
            "http_status": 401,
        }
        assert sum(e["physical_attempts"] for e in events) == len(calls)
        sources = list(db.scalars(select(ResearchSource).where(ResearchSource.run_id == run.id)))
        if all_fail:
            assert not sources and run.status == "partial_failed"
        else:
            assert sources and all(s.provider == "minimax" for s in sources)
            assert run.status == "completed"
    finally:
        db.close()
