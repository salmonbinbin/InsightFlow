"""Independent v6 integration: real validators/persistence; bounded external stubs."""

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
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

from app.models import Insight, ResearchFinding
from app.services import research_reader
from app.services import research_service as service
from app.services.research_client import ResearchLLMResult, ResearchLLMUsage

engine = _engine_fixture
SessionLocal = _session_fixture
fake_settings = _settings_fixture
FIXTURES = Path(__file__).parent / "fixtures" / "phase8_research_v5"


def task(question, quote, kind, product, platform, entry, action, tid="t1", constraints=None):
    assert quote in question
    return dict(
        id=tid,
        question_quote=quote,
        kind=kind,
        role="core",
        subject=dict(product=product, platform=platform, entry=entry),
        action=action,
        constraints=constraints or [],
        answer_mode="explicit_official_evidence",
    )


def install(monkeypatch, data, make_payload):
    rows = data["sources"]

    def provider(**kw):
        return _StubProviderClient(
            [
                dict(
                    id=s["provider_source_id"], url=s["url"], title=s["title"], content=s["snippet"]
                )
                for s in rows
            ]
        )

    monkeypatch.setattr(service, "ResearchProviderClient", provider)

    def read(url, allowed):
        assert allowed(url)
        s = next(s for s in rows if s["url"] == url)
        return research_reader.Snapshot(url, s["snippet"], "a" * 64, len(s["snippet"]))

    monkeypatch.setattr(research_reader, "read_official", read)
    for name in ["app.services.research_pipeline", "app.services.research_pipeline_v6"]:
        if name in sys.modules:
            monkeypatch.setattr(sys.modules[name], "read_official", read, raising=False)

    class Model:
        calls = 0

        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def generate_research(self, *, user_message, **kw):
            Model.calls += 1
            assert len(user_message["evidence_catalog"]) <= 100
            return ResearchLLMResult(make_payload(user_message), ResearchLLMUsage(10, 5))

    monkeypatch.setattr(service, "ResearchLLMClient", Model)
    return Model


def run_case(SessionLocal, question):
    db, seed = _seed_pipeline(SessionLocal)
    project = db.get(Insight, seed["insight_id"]).project_id
    run = service.start_research_run(
        db, project, seed["insight_id"], research_question=question, scope=None
    )
    return db, run


def test_v6_core_procedure_persists_fact_supports_and_recommendation(
    SessionLocal, fake_settings, monkeypatch
):
    q = "According to official Google Drive Help, what are the Android app steps to upload files? Cite only official Google Drive Help."
    body = "# Google Drive Android\n## Upload files\n1. Open Google Drive on Android.\n2. Tap New, then Upload.\n3. Find and tap the files to upload.\n4. Find uploaded files in My Drive.\n"
    data = {
        "sources": [
            dict(
                provider_source_id="d",
                url="https://support.google.com/drive/answer/2424368?hl=en&co=GENIE.Platform%3DAndroid",
                title="Google Drive Android upload files",
                domain="support.google.com",
                snippet=body,
            )
        ]
    }

    def output(m):
        ids = [u["evidence_id"] for u in m["evidence_catalog"] if u["step_index"]]
        return {
            "tasks": [
                task(
                    q,
                    "what are the Android app steps to upload files",
                    "procedure",
                    "Google Drive",
                    "Android",
                    "app",
                    "upload",
                )
            ],
            "evidence_ids": ids,
            "task_bindings": [dict(task_id="t1", evidence_ids=ids)],
            "analyses": [
                dict(
                    category="opportunity",
                    claim_kind="recommendation",
                    title="评估手动入口的易发现性",
                    description="可以评估入口是否易发现，并通过用户测试验证。",
                    scenario="手机用户主动上传文件",
                    supporting_evidence_ids=ids[:1],
                )
            ],
        }

    model = install(monkeypatch, data, output)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "completed", run.error_message
        assert run.prompt_version == "research-agent-v6"
        fs = db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)).all()
        facts = [f for f in fs if f.claim_kind == "fact"]
        recs = [f for f in fs if f.claim_kind == "recommendation"]
        assert len(facts) == 4 and len(recs) == 1
        assert set(recs[0].supporting_finding_ids) <= {str(f.id) for f in facts}
        assert recs[0].status == "not_verified" and not recs[0].source_refs
        assert all(f.evidence_location["text"] == f.description for f in facts)
        gaps = run.execution_trace["analysis_gaps"]
        assert {g["category"] for g in gaps} == {"market_pattern", "risk"}
        assert all(g["reason"] == "no_supported_candidate" for g in gaps)
        assert model.calls <= 3
    finally:
        db.close()


def test_v6_google_manual_and_automatic_are_separate_proofs(
    SessionLocal, fake_settings, monkeypatch
):
    data = json.loads((FIXTURES / "case-6.json").read_text())
    q = data["question"]
    data["sources"] = [s for s in data["sources"] if "/6193313" in s["url"]][:1]

    def output(m):
        steps = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if "Manually back up" in u["section"] and u["step_index"]
        ]
        auto = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if "You can automatically save" in u["text"]
        ]
        return {
            "tasks": [
                task(
                    q,
                    "what are the steps to manually back up one photo or video in the Android app",
                    "procedure",
                    "Google Photos",
                    "Android",
                    "app",
                    "manual_backup",
                ),
                task(
                    q,
                    "distinguish automatic backup",
                    "comparison",
                    "Google Photos",
                    "Android",
                    "app",
                    "distinguish_auto",
                    "t2",
                ),
            ],
            "evidence_ids": steps + auto,
            "task_bindings": [
                dict(task_id="t1", evidence_ids=steps),
                dict(task_id="t2", evidence_ids=steps + auto),
            ],
            "analyses": [],
        }

    install(monkeypatch, data, output)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "completed", run.coverage_report
    finally:
        db.close()


def test_v6_dropbox_full_steps_internal_quote_and_disabled_condition(
    SessionLocal, fake_settings, monkeypatch
):
    data = json.loads((FIXTURES / "case-7.json").read_text())
    q = data["question"]
    data["sources"] = [
        s
        for s in data["sources"]
        if s["url"] == "https://help.dropbox.com/create-upload/upload-mobile"
    ]

    def output(m):
        ids = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if u["section"] == "How to upload photos and videos"
            and (u["step_index"] or "don't have camera uploads enabled" in u["text"])
        ]
        return {
            "tasks": [
                task(
                    q,
                    "can mobile users manually add photos when camera uploads are disabled",
                    "capability",
                    "Dropbox",
                    "mobile",
                    "app",
                    "manual_upload",
                    constraints=[dict(kind="mode", value="camera_uploads_disabled")],
                )
            ],
            "evidence_ids": ids,
            "task_bindings": [dict(task_id="t1", evidence_ids=ids)],
            "analyses": [],
        }

    install(monkeypatch, data, output)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "completed", run.coverage_report
        fs = db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)).all()
        assert any('"+"' in f.description and f.status == "confirmed" for f in fs)
    finally:
        db.close()


@pytest.mark.parametrize("omit_options", [False, True])
def test_v6_apple_requires_mobile_options_and_shared_destination(
    SessionLocal, fake_settings, monkeypatch, omit_options
):
    data = json.loads((FIXTURES / "case-9.json").read_text())
    q = data["question"]
    data["sources"] = [s for s in data["sources"] if "mmb5dbaf376f" in s["url"]]

    def output(m):
        options = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if "On a phone or tablet: Choose" in u["text"]
        ]
        note = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if "iCloud Shared Photo Library, use" in u["text"]
        ]
        steps = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if u["section"] == "Upload photos" and u["step_index"]
        ]
        chosen = steps + note + ([] if omit_options else options)
        return {
            "tasks": [
                task(
                    q,
                    "which upload choices does iCloud.com show on a phone or tablet",
                    "entry_listing",
                    "iCloud",
                    "phone_or_tablet",
                    "icloud.com",
                    "upload_choices",
                ),
                task(
                    q,
                    "where should users upload to a Shared Photo Library",
                    "destination",
                    "iCloud",
                    "phone_or_tablet",
                    "shared_photo_library",
                    "upload_destination",
                    "t2",
                ),
            ],
            "evidence_ids": chosen,
            "task_bindings": [
                dict(task_id="t1", evidence_ids=steps + ([] if omit_options else options)),
                dict(task_id="t2", evidence_ids=note),
            ],
            "analyses": [],
        }

    install(monkeypatch, data, output)
    db, run = run_case(SessionLocal, q)
    try:
        if omit_options:
            assert run.status != "completed"
        else:
            assert run.status == "completed", run.coverage_report
    finally:
        db.close()


def test_v6_capability_note_cannot_complete_when_its_official_procedure_is_unselected(
    SessionLocal, fake_settings, monkeypatch
):
    data = json.loads((FIXTURES / "case-7.json").read_text())
    q = data["question"]
    data["sources"] = [
        s
        for s in data["sources"]
        if s["url"] == "https://help.dropbox.com/create-upload/upload-mobile"
    ]

    def output(m):
        ids = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if "don't have camera uploads enabled" in u["text"]
        ]
        return dict(
            tasks=[
                task(
                    q,
                    "can mobile users manually add photos when camera uploads are disabled",
                    "capability",
                    "Dropbox",
                    "mobile",
                    "app",
                    "manual_upload",
                    constraints=[dict(kind="condition", value="camera_uploads_disabled")],
                )
            ],
            evidence_ids=ids,
            task_bindings=[dict(task_id="t1", evidence_ids=ids)],
            analyses=[],
        )

    install(monkeypatch, data, output)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "partial_failed", run.coverage_report
        assert not run.coverage_report["overall_completable"]
    finally:
        db.close()


def test_v6_comparison_can_join_an_independently_proven_manual_task_with_automatic_fact(
    SessionLocal, fake_settings, monkeypatch
):
    data = json.loads((FIXTURES / "case-6.json").read_text())
    q = data["question"]
    data["sources"] = [s for s in data["sources"] if "/6193313" in s["url"]][:1]

    def output(m):
        steps = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if "Manually back up" in u["section"] and u["step_index"]
        ]
        auto = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if "You can automatically save" in u["text"]
        ]
        return dict(
            tasks=[
                task(
                    q,
                    "what are the steps to manually back up one photo or video in the Android app",
                    "procedure",
                    "Google Photos",
                    "Android",
                    "app",
                    "manual_backup",
                ),
                task(
                    q,
                    "distinguish automatic backup",
                    "comparison",
                    "Google Photos",
                    "Android",
                    "app",
                    "distinguish_auto",
                    "t2",
                ),
            ],
            evidence_ids=steps + auto,
            task_bindings=[
                dict(task_id="t1", evidence_ids=steps),
                dict(task_id="t2", evidence_ids=auto),
            ],
            analyses=[],
        )

    install(monkeypatch, data, output)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "completed", run.coverage_report
    finally:
        db.close()


def test_v6_multi_product_official_inventory_fetches_each_requested_vendor_and_persists_pattern(
    SessionLocal, fake_settings, monkeypatch
):
    q = "Compare manual upload in Google Photos and Dropbox mobile. Give sample patterns, opportunities and risks. Cite only official help."
    rows = [
        dict(
            provider_source_id="g",
            url="https://support.google.com/photos/answer/6193313",
            title="Google Photos Android manual upload",
            domain="support.google.com",
            snippet="# Google Photos Android\n## Manually upload\n1. Open Google Photos on Android and upload a photo.\n",
        ),
        dict(
            provider_source_id="d",
            url="https://help.dropbox.com/create-upload/upload-mobile",
            title="Dropbox mobile manual upload",
            domain="help.dropbox.com",
            snippet="# Dropbox mobile\n## Manually upload\n1. Open Dropbox on mobile and upload a photo.\n",
        ),
    ]

    def output(m):
        ids = [u["evidence_id"] for u in m["evidence_catalog"] if u["step_index"]]
        return dict(
            tasks=[
                task(
                    q,
                    "Compare manual upload in Google Photos and Dropbox mobile",
                    "comparison",
                    None,
                    "mobile",
                    "app",
                    "upload",
                    "t1",
                ),
                task(
                    q,
                    "Give sample patterns, opportunities and risks",
                    "analysis",
                    None,
                    "mobile",
                    "app",
                    "analysis",
                    "t2",
                ),
            ],
            evidence_ids=ids,
            task_bindings=[
                dict(task_id="t1", evidence_ids=ids),
                dict(task_id="t2", evidence_ids=ids),
            ],
            analyses=[
                dict(
                    category="market_pattern",
                    claim_kind="inference",
                    title="样本观察",
                    description="本次两款产品样本均描述了用户主动打开上传入口的操作；仅作为样本流程观察。",
                    scenario="手机用户主动上传照片",
                    supporting_evidence_ids=ids,
                )
            ],
        )

    install(monkeypatch, dict(sources=rows), output)
    queries = []

    def provider(**kw):
        class Provider(_StubProviderClient):
            def search(self, query):
                queries.append(query)
                chosen = [
                    s
                    for s in rows
                    if ("site:" + s["domain"]) in query
                    and (
                        ("google photos" in query.lower() and s["provider_source_id"] == "g")
                        or ("dropbox" in query.lower() and s["provider_source_id"] == "d")
                    )
                ]
                self._sources = [
                    dict(
                        id=s["provider_source_id"],
                        url=s["url"],
                        title=s["title"],
                        content=s["snippet"],
                    )
                    for s in chosen
                ]
                return _StubProviderClient(self._sources).search(query)

        return Provider([])

    monkeypatch.setattr(service, "ResearchProviderClient", provider)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.source_count == 2, queries
        fs = db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)).all()
        patterns = [f for f in fs if f.category == "market_pattern" and f.claim_kind == "inference"]
        assert len(patterns) == 1
        assert len(patterns[0].supporting_finding_ids) == 2
        assert patterns[0].status == "not_verified" and not patterns[0].source_refs
        assert run.execution_trace["budgets"]["logical_searches"] <= 3
        assert run.execution_trace["budgets"]["official_reads"] <= 5
        assert run.execution_trace["budgets"]["llm_calls"] <= 3
    finally:
        db.close()


def _small_drive_case():
    q = "In Google Drive Android, how can a user upload files? Cite only official Google Drive Help."
    data = dict(
        sources=[
            dict(
                provider_source_id="drive",
                url="https://support.google.com/drive/answer/2424368",
                title="Google Drive Android upload",
                domain="support.google.com",
                snippet="# Google Drive Android\n## Upload files\n1. Open Google Drive on Android.\n2. Tap Upload.\n",
            )
        ]
    )

    def output(m):
        ids = [u["evidence_id"] for u in m["evidence_catalog"] if u["step_index"]]
        return dict(
            tasks=[
                task(
                    q,
                    "how can a user upload files",
                    "procedure",
                    "Google Drive",
                    "Android",
                    "app",
                    "upload",
                )
            ],
            evidence_ids=ids,
            task_bindings=[dict(task_id="t1", evidence_ids=ids)],
            analyses=[
                dict(
                    category="opportunity",
                    claim_kind="recommendation",
                    title="评估入口",
                    description="可以评估主动上传入口是否易发现，并通过用户测试验证。",
                    scenario="手机用户主动上传文件",
                    supporting_evidence_ids=ids[:1],
                )
            ],
        )

    return q, data, output


def test_v6_task_contract_failure_keeps_independently_valid_facts_and_supported_analysis(
    SessionLocal, fake_settings, monkeypatch
):
    q, data, output = _small_drive_case()

    def invalid_task(m):
        p = output(m)
        p["tasks"][0]["subject"]["product"] = "Unmentioned Product"
        return p

    install(monkeypatch, data, invalid_task)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "partial_failed"
        rows = db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)).all()
        assert sum(f.claim_kind == "fact" and f.status == "confirmed" for f in rows) == 2
        assert (
            sum(f.claim_kind == "recommendation" and f.status == "not_verified" for f in rows) == 1
        )
        assert not run.coverage_report["overall_completable"]
        assert any(
            e.get("task_contract_failure", {}).get("code") == "task_subject_not_in_question"
            for e in run.execution_trace["events"]
        )
    finally:
        db.close()


def test_v6_failed_official_read_cannot_confirm_a_search_excerpt(
    SessionLocal, fake_settings, monkeypatch
):
    q, data, output = _small_drive_case()
    install(monkeypatch, data, output)

    def failed_read(*a):
        raise research_reader.ReadError("fetch_timeout")

    monkeypatch.setattr(research_reader, "read_official", failed_read)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "partial_failed"
        rows = db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)).all()
        facts = [f for f in rows if f.claim_kind == "fact"]
        assert facts and all(f.status == "not_verified" and not f.source_refs for f in facts)
        assert all(
            f.evidence_location["first_failed_guard"] == "official_snapshot_unavailable"
            for f in facts
        )
        assert not any(f.claim_kind in {"inference", "recommendation"} for f in rows)
    finally:
        db.close()


def test_v6_equal_fact_coverage_preserves_a_later_supported_analysis(
    SessionLocal, fake_settings, monkeypatch
):
    q, data, output = _small_drive_case()
    calls = 0

    def later_analysis(m):
        nonlocal calls
        calls += 1
        p = output(m)
        p["tasks"][0]["subject"]["product"] = "Unmentioned Product"
        if calls == 1:
            p["analyses"] = []
        return p

    install(monkeypatch, data, later_analysis)
    db, run = run_case(SessionLocal, q)
    try:
        rows = db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)).all()
        assert any(f.claim_kind == "recommendation" for f in rows)
        assert run.status == "partial_failed" and not run.coverage_report["overall_completable"]
    finally:
        db.close()
