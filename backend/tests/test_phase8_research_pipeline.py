"""Independent final-path checks. All inputs are synthetic; no network."""

import sys

from test_phase8_research import (
    SessionLocal as _session_fixture,
)
from test_phase8_research import (
    _seed_pipeline,
    _StubProviderClient,
)
from test_phase8_research import (
    engine as _engine_fixture,
)
from test_phase8_research import (
    fake_settings as _settings_fixture,
)

from app.models import Insight
from app.services import research_reader
from app.services import research_service as service
from app.services.research_client import ResearchLLMResult, ResearchLLMUsage

engine = _engine_fixture
SessionLocal = _session_fixture
fake_settings = _settings_fixture

BODY = "# Google Drive Android\n## Upload files\n1. On your Android phone, open Google Drive.\n2. Tap New and then Upload.\n3. Find and tap the files you want to upload.\n4. Find uploaded files in My Drive.\n"
URL = "https://support.google.com/drive/answer/2424368?hl=en&co=GENIE.Platform%3DAndroid"


def _install(monkeypatch, body=BODY, payload=None):
    def provider(**kwargs):
        return _StubProviderClient(
            [
                {
                    "id": "official-1",
                    "url": URL,
                    "title": "Google Drive Android upload files",
                    "content": body,
                }
            ]
        )

    monkeypatch.setattr(service, "ResearchProviderClient", provider)

    def read(url, allowed):
        assert allowed(url)
        return research_reader.Snapshot(url, body, "a" * 64, len(body))

    monkeypatch.setattr(research_reader, "read_official", read)
    if "app.services.research_pipeline" in sys.modules:
        monkeypatch.setattr(sys.modules["app.services.research_pipeline"], "read_official", read)

    class Model:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def generate_research(self, *, user_message, **kwargs):
            selected = [
                u["evidence_id"]
                for u in user_message.get("evidence_catalog", [])
                if u["step_index"]
            ]
            return ResearchLLMResult(
                payload
                or {
                    "summary": "Synthetic internal context",
                    "confidence": 0.5,
                    "evidence_ids": selected,
                },
                ResearchLLMUsage(10, 5),
            )

    monkeypatch.setattr(service, "ResearchLLMClient", Model)


def test_legal_roaming_fact_cannot_complete_manual_steps(SessionLocal, fake_settings, monkeypatch):
    body = "Google Drive Android manual upload. Turn on Backup while roaming."
    _install(
        monkeypatch,
        body,
        {
            "summary": "Synthetic",
            "confidence": 0.5,
            "competitors": [
                {
                    "name": "Google Drive",
                    "description": 'Evidence: "Turn on Backup while roaming."',
                    "confidence": 0.5,
                    "source_refs": [
                        {
                            "provider_source_id": "official-1",
                            "url": URL,
                            "title": "Google Drive Android upload files",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )
    db, seed = _seed_pipeline(SessionLocal)
    try:
        project = db.get(Insight, seed["insight_id"]).project_id
        run = service.start_research_run(
            db,
            project,
            seed["insight_id"],
            research_question="What are the Android app steps to manually upload photos in Google Drive? Cite only official Google Drive Help.",
            scope=None,
        )
        assert run.status != "completed"
        assert run.coverage_report["overall_completable"] is False
    finally:
        db.close()


def test_complete_saved_procedure_uses_deterministic_citations(
    SessionLocal, fake_settings, monkeypatch
):
    _install(monkeypatch)
    db, seed = _seed_pipeline(SessionLocal)
    try:
        project = db.get(Insight, seed["insight_id"]).project_id
        run = service.start_research_run(
            db,
            project,
            seed["insight_id"],
            research_question="According to official Google Drive Help, what are the Android app steps to upload files? Cite only official Google Drive Help.",
            scope=None,
        )
        assert run.status == "completed"
        assert run.coverage_report["overall_completable"] is True
        assert run.task_contract["question"] == run.research_question
        _, findings = service.list_findings_for_run(db, run_id=run.id, limit=50, offset=0)
        quotes = [
            service._extract_evidence_quote(f.description)
            for f in findings
            if f.category == "competitor"
        ]
        assert len(quotes) == 4
        assert all(quote in BODY for quote in quotes)
        assert run.execution_trace["evidence_locations"]
    finally:
        db.close()


def test_one_valid_step_cannot_complete_saved_four_step_procedure(
    SessionLocal, fake_settings, monkeypatch
):
    _install(monkeypatch)
    original = service.ResearchLLMClient.generate_research

    def one_step(self, **kwargs):
        result = original(self, **kwargs)
        result.payload["evidence_ids"] = result.payload["evidence_ids"][:1]
        return result

    monkeypatch.setattr(service.ResearchLLMClient, "generate_research", one_step)
    db, seed = _seed_pipeline(SessionLocal)
    try:
        project = db.get(Insight, seed["insight_id"]).project_id
        run = service.start_research_run(
            db,
            project,
            seed["insight_id"],
            research_question="What are the Android app steps to upload files? Cite only official Google Drive Help.",
            scope=None,
        )
        assert run.status == "partial_failed"
        assert run.coverage_report["overall_completable"] is False
        assert run.retry_count == 2
        assert run.attempt_count == 4
        assert run.input_tokens == 30
        _, findings = service.list_findings_for_run(db, run_id=run.id, limit=50, offset=0)
        assert sum(f.status == "confirmed" for f in findings) == 1
        assert any("missing step" in item["gap"] for item in run.coverage_report["items"])
    finally:
        db.close()
