"""v7 executes real validation/persistence with bounded external boundaries."""

import pytest
from sqlalchemy import select
from test_phase8_research import (
    SessionLocal as _SessionLocal,
)
from test_phase8_research import (
    _seed_pipeline,
    _StubProviderClient,
)
from test_phase8_research import (
    engine as _engine,
)
from test_phase8_research import (
    fake_settings as _settings,
)

from app.models import Insight, ResearchFinding
from app.services import research_reader
from app.services import research_service as service
from app.services.research_client import (
    ResearchLLMProtocolError,
    ResearchLLMResult,
    ResearchLLMUsage,
)
from app.services.research_spec_v7 import question_parts

engine = _engine
SessionLocal = _SessionLocal
fake_settings = _settings
Q = "How does OneDrive iPhone manually upload photos?"
BODY = "# OneDrive iOS\n## Manually upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
URL = "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios"


def plan(q):
    return dict(
        requirements=[
            dict(
                id="r1",
                anchor_ids=[a["id"] for a in question_parts(q)["anchors"]],
                kind="procedure",
                products=["OneDrive"],
                platform="iPhone",
                entry="app",
                action="upload",
                object="photos",
                mode="manual",
                quantity="unspecified",
                condition_quotes=[],
                version=None,
                analysis_facets=[],
            )
        ],
        queries=[{"id": "r1", "text": "OneDrive iPhone manually upload photos"}],
    )


def install(monkeypatch, make_payload, read_ok=True):
    calls = []
    searches = []
    reads = []

    def provider(**kw):
        client = _StubProviderClient([dict(id="s", url=URL, title="OneDrive iOS", content=BODY)])
        original = client.search

        def search(q):
            searches.append(q)
            return original(q)

        client.search = search
        return client

    monkeypatch.setattr(service, "ResearchProviderClient", provider)

    def read(url, allowed):
        reads.append(url)
        assert allowed(url)
        if not read_ok:
            raise research_reader.ReadError("http_failure")
        return research_reader.Snapshot(url, BODY, "a" * 64, len(BODY))

    monkeypatch.setattr(research_reader, "read_official", read)

    class Model:
        def __init__(self, **kw):
            self.module = kw["prompt_module"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def generate_research(self, *, user_message, **kw):
            calls.append(user_message)
            assert "search_results" in user_message and "evidence_catalog" in user_message
            assert len(user_message["evidence_catalog"]) <= 100
            return ResearchLLMResult(
                make_payload(user_message, len(calls)), ResearchLLMUsage(10, 5)
            )

    monkeypatch.setattr(service, "ResearchLLMClient", Model)
    return calls, searches, reads


def output(m, n):
    if m["stage"] == "planning":
        return plan(m["research_question"])
    ids = [u["evidence_id"] for u in m["evidence_catalog"]]
    selected = dict(
        evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)], analyses=[]
    )
    return plan(m["research_question"]) | selected if m["stage"] == "joint" else selected


def run_case(SessionLocal, q=Q):
    db, seed = _seed_pipeline(SessionLocal)
    pid = db.get(Insight, seed["insight_id"]).project_id
    run = service.start_research_run(
        db,
        pid,
        seed["insight_id"],
        research_question=q,
        scope=None,
        prompt_version="research-agent-v7",
    )
    return db, run


def test_v7_freezes_plan_before_search_and_persists_extracts_and_answer(
    SessionLocal, fake_settings, monkeypatch
):
    calls, searches, reads = install(monkeypatch, output)
    db, run = run_case(SessionLocal)
    try:
        assert run.status == "completed", run.error_message
        assert run.prompt_version == "research-agent-v7"
        assert len(calls) == 2 and calls[0]["stage"] == "planning"
        assert run.task_contract["version"] == "research-spec-v7"
        assert run.coverage_report["answer"]["version"] == "research-answer-v7"
        rows = list(db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)))
        assert len([f for f in rows if f.claim_kind == "fact" and f.status == "confirmed"]) == 2
        assert run.coverage_report["items"][0]["adopted_attempt"] == 2
        assert run.execution_trace["budgets"]["llm_calls"] == 2
        assert len(searches) <= 3 and len(reads) <= 5
    finally:
        db.close()


def test_third_protocol_failure_preserves_second_partial_support(
    SessionLocal, fake_settings, monkeypatch
):
    def partial(m, n):
        if m["stage"] == "planning":
            return plan(m["research_question"])
        if n == 3:
            raise ResearchLLMProtocolError(
                "missing tool", response_metadata={"stop_reason": "end_turn"}
            )
        ids = [m["evidence_catalog"][0]["evidence_id"]]
        return dict(
            evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)], analyses=[]
        )

    calls, _, _ = install(monkeypatch, partial)
    db, run = run_case(SessionLocal, q="How does OneDrive iPhone manually upload multiple photos?")
    try:
        assert len(calls) == 3 and run.status == "partial_failed"
        assert run.coverage_report["items"][0]["adopted_attempt"] == 2
        assert run.coverage_report["items"][0]["evidence_ids"]
        assert run.execution_trace["last_failure"]["code"] == "protocol_failure"
        assert run.execution_trace["last_failure"]["attempt"] == 3
    finally:
        db.close()


def test_planning_protocol_failures_consume_total_three_calls_without_search(
    SessionLocal, fake_settings, monkeypatch
):
    def fail(m, n):
        raise ResearchLLMProtocolError("no tool")

    calls, searches, reads = install(monkeypatch, fail)
    db, run = run_case(SessionLocal)
    try:
        assert len(calls) == 3 and len(searches) <= 3 and len(reads) <= 5
        assert run.status == "partial_failed" and run.finding_count == 0
        assert run.execution_trace["budgets"]["llm_calls"] == 3
    finally:
        db.close()


def test_provider_snippet_cannot_replace_failed_official_read(
    SessionLocal, fake_settings, monkeypatch
):
    install(monkeypatch, output, read_ok=False)
    db, run = run_case(SessionLocal)
    try:
        assert run.status == "partial_failed"
        assert not list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.status == "confirmed"
                )
            )
        )
    finally:
        db.close()


def test_unknown_requested_version_rejected_by_request_schema():
    from pydantic import ValidationError

    from app.schemas.research import ResearchRunRequest

    with pytest.raises(ValidationError):
        ResearchRunRequest(research_question=Q, prompt_version="invented-version")


def test_invalid_query_plan_is_not_frozen_and_can_be_repaired(
    SessionLocal, fake_settings, monkeypatch
):
    def repair(m, n):
        if n == 1:
            p = plan(Q)
            p["queries"][0]["id"] = "unknown"
            return p
        return output(m, n)

    calls, _, _ = install(monkeypatch, repair)
    db, run = run_case(SessionLocal)
    try:
        assert [c["stage"] for c in calls] == ["planning", "joint"]
        assert run.status == "completed", run.error_message
    finally:
        db.close()


def test_chinese_query_keeps_original_action_alongside_translation():
    from app.services.research_pipeline_v7 import queries
    from app.services.research_spec_v7 import compile_spec

    q = "OneDrive iPhone 用户如何手动上传照片？"
    p = plan(q)
    s = compile_spec(q, p)
    rules = [service._OFFICIAL_VENDOR_RULES["onedrive"]]
    result = queries(s, rules)
    assert "手动上传照片" in result[0]["text"] and "upload" in result[0]["text"]


def test_rejected_optional_analysis_does_not_erase_independently_valid_facts(
    SessionLocal, fake_settings, monkeypatch
):
    def invalid_analysis(m, n):
        result = output(m, n)
        if m["stage"] != "planning":
            result["analyses"] = [{"facet": "unsupported", "status": "confirmed"}]
        return result

    install(monkeypatch, invalid_analysis)
    db, run = run_case(SessionLocal)
    try:
        assert run.finding_count == 2 and run.status == "completed"
        assert run.execution_trace["events"][-1]["analysis_rejections"]
        assert not list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.claim_kind != "fact"
                )
            )
        )
    finally:
        db.close()


def test_server_flow_closure_adds_missing_same_flow_step_without_extra_llm(
    SessionLocal, fake_settings, monkeypatch
):
    def omitted(m, n):
        result = output(m, n)
        if m["stage"] != "planning":
            ids = [m["evidence_catalog"][0]["evidence_id"]]
            result.update(evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)])
        return result

    calls, _, _ = install(monkeypatch, omitted)
    db, run = run_case(SessionLocal)
    try:
        assert run.status == "completed", run.error_message
        assert len(calls) == 2
        assert len(run.coverage_report["items"][0]["evidence_ids"]) == 2
        assert run.execution_trace["events"][-1]["selection_closure"]["added_ids"]
    finally:
        db.close()


def test_two_failed_plans_can_recover_facts_on_third_and_last_joint_call(
    SessionLocal, fake_settings, monkeypatch
):
    def recover(m, n):
        if n < 3:
            raise ResearchLLMProtocolError("no tool", response_metadata={"stop_reason": "end_turn"})
        return output(m, n)

    calls, searches, reads = install(monkeypatch, recover)
    db, run = run_case(SessionLocal)
    try:
        assert [m["stage"] for m in calls] == ["planning", "joint", "joint"]
        assert run.status == "completed", run.error_message
        assert run.finding_count == 2
        assert run.execution_trace["budgets"]["llm_calls"] == 3
        assert len(searches) <= 3 and len(reads) <= 5
        assert run.coverage_report["items"][0]["adopted_attempt"] == 3
    finally:
        db.close()


def test_general_capability_seed_is_expanded_to_its_published_flow(
    SessionLocal, fake_settings, monkeypatch
):
    import sys

    monkeypatch.setattr(sys.modules[__name__], "BODY", BODY + "You can manually upload photos.\n")

    def seed_only(m, n):
        if m["stage"] == "planning":
            p = plan(m["research_question"])
            p["requirements"][0]["kind"] = "capability"
            return p
        ids = [u["evidence_id"] for u in m["evidence_catalog"] if not u["step_index"]]
        return dict(
            evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)], analyses=[]
        )

    calls, _, _ = install(monkeypatch, seed_only)
    db, run = run_case(SessionLocal, q="Can OneDrive iPhone manually upload photos?")
    try:
        assert run.status == "completed", run.error_message
        assert len(calls) == 2 and run.finding_count == 3
    finally:
        db.close()


def test_rejected_analysis_candidates_do_not_block_third_call_repair(
    SessionLocal, fake_settings, monkeypatch
):
    q = "How does OneDrive iPhone manually upload photos? Give one opportunity for the upload scenario."

    def response(m, n):
        if n == 1:
            p = plan(q)
            p["requirements"][0]["anchor_ids"] = ["q1"]
            p["requirements"].append(
                p["requirements"][0]
                | dict(id="a1", anchor_ids=["q2"], kind="analysis", analysis_facets=["opportunity"])
            )
            return p
        ids = [e["evidence_id"] for e in m["evidence_catalog"]]
        base = dict(
            requirement_id="a1",
            facet="opportunity",
            operator="proposed_test",
            premises=[dict(requirement_id="r1", evidence_ids=ids)],
            scenario="upload scenario",
            proposal="Test this documented upload and record cancellations and repeat selections.",
        )
        cs = (
            [
                base | dict(scenario="not in the original input", proposal=f"Test {i}")
                for i in range(8)
            ]
            if n == 2
            else [base]
        )
        return dict(
            evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)], analyses=cs
        )

    calls, _, _ = install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "completed", run.error_message
        assert len(calls) == 3 and run.execution_trace["budgets"]["llm_calls"] == 3
        assert (
            len(
                [
                    f
                    for f in db.scalars(
                        select(ResearchFinding).where(ResearchFinding.run_id == run.id)
                    )
                    if f.claim_kind == "recommendation"
                ]
            )
            == 1
        )
    finally:
        db.close()


def test_complete_source_policy_candidate_is_reclassified_without_spending_repair_call(
    SessionLocal, fake_settings, monkeypatch
):
    q = "Using only official Microsoft Support, " + Q

    def response(m, n):
        result = output(m, n)
        if m["stage"] in {"planning", "joint"}:
            result["requirements"].append(
                result["requirements"][0]
                | dict(
                    id="policy",
                    kind="condition",
                    action="cite",
                    object="official documentation",
                    condition_quotes=["Using only official Microsoft Support"],
                )
            )
        return result

    calls, _, _ = install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "completed", run.error_message
        assert len(calls) == 2
        assert "policy" not in {r["id"] for r in run.task_contract["requirements"]}
        assert "source_policy" in __import__("json").dumps(run.execution_trace)
    finally:
        db.close()


def test_planning_receives_independent_input_obligations_and_flow_choices_only_from_offered_units(
    SessionLocal, fake_settings, monkeypatch
):
    calls, _, _ = install(monkeypatch, output)
    db, run = run_case(SessionLocal)
    try:
        assert calls[0]["input_obligations"]
        assert any(o["action_canon"] == "upload" for o in calls[0]["input_obligations"])
        catalog_ids = {e["evidence_id"] for e in calls[1]["evidence_catalog"]}
        assert calls[1]["flow_catalog"]
        assert all(set(f["evidence_ids"]) <= catalog_ids for f in calls[1]["flow_catalog"])
    finally:
        db.close()


def test_known_rejected_relation_binding_is_local_but_truly_unknown_binding_stays_strict():
    from test_phase8_spec_v7_supervisor import requirement
    from test_phase8_support_v7_supervisor import inventory

    from app.services.research_pipeline_v7 import _selection_closure
    from app.services.research_spec_v7 import SpecError, compile_spec

    q = "Can OneDrive iPhone upload files and delete photos?"
    base = requirement("q1") | dict(
        products=["OneDrive"], platform="iPhone", kind="capability", mode="unspecified"
    )
    s = compile_spec(
        q,
        dict(
            requirements=[
                base,
                base | dict(id="d", action="delete", object="photos"),
                base | dict(id="bad", object="photos"),
            ],
            queries=[],
        ),
    )
    _, _, _, entries = inventory(
        "# OneDrive iOS\n## Upload files\n1. Open OneDrive on iPhone.\n2. Choose files and tap Upload.\n"
    )
    ids = list(entries)
    payload = dict(
        evidence_ids=ids,
        bindings=[
            dict(requirement_id="r1", evidence_ids=ids),
            dict(requirement_id="bad", evidence_ids=ids),
        ],
        analyses=[],
    )
    closed, _ = _selection_closure(s, payload, entries)
    assert all(b["requirement_id"] != "bad" for b in closed["bindings"])
    payload["bindings"][-1]["requirement_id"] = "never-declared"
    with pytest.raises(SpecError, match="binding_requirement"):
        _selection_closure(s, payload, entries)


def test_temporarily_uncovered_analysis_can_recover_after_fact_group_is_supplied(
    SessionLocal, fake_settings, monkeypatch
):
    q = "How does OneDrive iPhone manually upload photos? Give one opportunity for the upload scenario."

    def response(m, n):
        if n == 1:
            p = plan(q)
            p["requirements"][0]["anchor_ids"] = ["q1"]
            p["requirements"].append(
                p["requirements"][0]
                | dict(id="a1", anchor_ids=["q2"], kind="analysis", analysis_facets=["opportunity"])
            )
            return p
        ids = [e["evidence_id"] for e in m["evidence_catalog"]]
        c = dict(
            requirement_id="a1",
            facet="opportunity",
            operator="proposed_test",
            premises=[dict(requirement_id="r1", evidence_ids=[])],
            scenario="upload scenario",
            proposal="Test the documented flow and record repeat selections.",
        )
        return dict(
            evidence_ids=[] if n == 2 else ids,
            bindings=[dict(requirement_id="r1", evidence_ids=[] if n == 2 else ids)],
            analyses=[c] if n == 2 else [],
        )

    calls, _, _ = install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "completed", run.error_message
        assert len(calls) == 3
        rows = list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.claim_kind == "recommendation"
                )
            )
        )
        assert len(rows) == 1
    finally:
        db.close()


def test_later_bad_candidates_do_not_evict_a_legal_partial_analysis(
    SessionLocal, fake_settings, monkeypatch
):
    q = "How does OneDrive iPhone manually upload photos? Give opportunities and risks for the upload scenario."

    def response(m, n):
        if n == 1:
            p = plan(q)
            p["requirements"][0]["anchor_ids"] = ["q1"]
            p["requirements"].append(
                p["requirements"][0]
                | dict(
                    id="a1",
                    anchor_ids=["q2"],
                    kind="analysis",
                    analysis_facets=["opportunity", "risk"],
                )
            )
            return p
        ids = [e["evidence_id"] for e in m["evidence_catalog"]]
        base = dict(
            requirement_id="a1",
            facet="opportunity",
            operator="proposed_test",
            premises=[dict(requirement_id="r1", evidence_ids=ids)],
            scenario="upload scenario",
            proposal="Test the documented flow.",
        )
        bad = [
            base | dict(facet="risk", scenario="not an input scenario", proposal=f"Test {i}")
            for i in range(7 if n == 2 else 8)
        ]
        return dict(
            evidence_ids=ids,
            bindings=[dict(requirement_id="r1", evidence_ids=ids)],
            analyses=[base] + bad if n == 2 else bad,
        )

    install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "partial_failed"
        rows = list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.claim_kind == "recommendation"
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].evidence_location["facet"] == "opportunity"
    finally:
        db.close()


@pytest.mark.parametrize("mutation", ["duplicate", "mixed", "partial"])
def test_policy_reclassification_preserves_schema_and_real_condition_guards(
    SessionLocal, fake_settings, monkeypatch, mutation
):
    q = "Using only official Microsoft Support, " + Q

    def response(m, n):
        result = output(m, n)
        if m["stage"] in {"planning", "joint"}:
            policy = result["requirements"][0] | dict(
                id="policy",
                kind="condition",
                action="cite",
                object="official documentation",
                condition_quotes=["Using only official Microsoft Support"],
            )
            if mutation == "duplicate":
                policy["id"] = result["requirements"][0]["id"]
            elif mutation == "mixed":
                policy["condition_quotes"].append("when storage is insufficient")
            else:
                policy["condition_quotes"] = ["official Microsoft Support"]
            result["requirements"].append(policy)
        return result

    calls, _, _ = install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == "partial_failed"
        assert len(calls) == 3
        assert not run.task_contract
    finally:
        db.close()


def two_fact_plan(q):
    anchors = question_parts(q)["anchors"]
    assert len(anchors) == 3, anchors
    p = plan(q)
    r = p["requirements"][0] | dict(kind="capability", mode="unspecified", anchor_ids=["q1"])
    p["requirements"] = [
        r,
        r | dict(id="r2", anchor_ids=["q2"], action="delete", object="files"),
        r
        | dict(
            id="a1",
            anchor_ids=["q3"],
            kind="analysis",
            action=None,
            object=None,
            analysis_facets=["opportunity", "risk"],
        ),
    ]
    p["queries"] = [
        dict(id="r1", text="OneDrive iPhone upload photos"),
        dict(id="r2", text="OneDrive iPhone delete files"),
    ]
    return p


def two_fact_question():
    return (
        "Can OneDrive iPhone upload photos? Can OneDrive iPhone delete files? "
        "Give opportunities and risks for these scenarios."
    )


def proposal(rid, facet):
    return dict(
        requirement_id="a1",
        facet=facet,
        operator="proposed_test",
        premises=[dict(requirement_id=rid, evidence_ids=[])],
        scenario="these scenarios",
        proposal="Test the documented task.",
    )


def text_sources(monkeypatch, response, rows):
    import hashlib

    calls, searches, reads = install(monkeypatch, response)
    bodies = {r["url"]: r["content"] for r in rows}

    def provider(**kw):
        return _StubProviderClient(rows)

    def read(url, allowed):
        assert allowed(url)
        body = bodies[url]
        return research_reader.Snapshot(
            url, body, hashlib.sha256(body.encode()).hexdigest(), len(body.encode())
        )

    monkeypatch.setattr(service, "ResearchProviderClient", provider)
    monkeypatch.setattr(research_reader, "read_official", read)
    return calls


def capability_body(n):
    return (
        "# OneDrive iOS\n## Upload photos\n"
        + "".join(f"OneDrive on iPhone can upload photos using option {i}.\n" for i in range(n))
        + "## Delete files\nOneDrive on iPhone can delete files.\n"
    )


def ids_for(m, sid, text):
    return [
        e["evidence_id"]
        for e in m["evidence_catalog"]
        if e["provider_source_id"] == sid and text in e["text"]
    ]


def test_conflicting_ledger_revokes_only_dependent_historical_analysis(
    SessionLocal, fake_settings, monkeypatch
):
    q = two_fact_question()
    remembered = {}

    def response(m, n):
        if n == 1:
            return two_fact_plan(q)
        if n == 2:
            a, b = (
                ids_for(m, "positive", "can upload photos"),
                ids_for(m, "positive", "can delete files"),
            )
            assert len(a) == len(b) == 1
            remembered["delete"] = b
            return dict(
                evidence_ids=a + b,
                bindings=[
                    dict(requirement_id="r1", evidence_ids=a),
                    dict(requirement_id="r2", evidence_ids=b),
                ],
                analyses=[proposal("r1", "opportunity"), proposal("r2", "opportunity")],
            )
        a = ids_for(m, "negative", "cannot upload photos")
        assert len(a) == 1
        return dict(
            evidence_ids=a, bindings=[dict(requirement_id="r1", evidence_ids=a)], analyses=[]
        )

    calls = text_sources(
        monkeypatch,
        response,
        [
            dict(id="positive", url=URL, title="OneDrive iOS", content=capability_body(1)),
            dict(
                id="negative",
                url="https://support.microsoft.com/en-us/onedrive/v7-test-negative-upload",
                title="OneDrive iOS",
                content="# OneDrive iOS\n## Upload photos\nOneDrive on iPhone cannot upload photos.\n",
            ),
        ],
    )
    db, run = run_case(SessionLocal, q)
    try:
        assert len(calls) == 3
        assert run.status == "partial_failed"
        items = {i["requirement_id"]: i for i in run.coverage_report["items"]}
        assert items["r1"]["answer_polarity"] == "conflicting"
        assert items["r2"]["status"] == "covered"
        rows = list(db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)))
        kept = [f for f in rows if f.claim_kind == "recommendation"]
        assert len(kept) == 1
        assert kept[0].evidence_location["premises"] == [
            dict(requirement_id="r2", evidence_ids=remembered["delete"])
        ]
        assert set(kept[0].supporting_finding_ids) == {
            str(f.id) for f in rows if "can delete files" in f.description
        }
        assert len(run.coverage_report["answer"]["analyses"]) == 1
    finally:
        db.close()


@pytest.mark.parametrize("diagnostic_count,expected", [(38, 2), (39, 0)])
def test_resolved_analysis_union_includes_current_selection_and_retained_ledger(
    SessionLocal, fake_settings, monkeypatch, diagnostic_count, expected
):
    from copy import deepcopy

    q = two_fact_question()
    submitted = []

    def response(m, n):
        if n == 1:
            return two_fact_plan(q)
        a, b = (
            ids_for(m, "positive", "can upload photos"),
            ids_for(m, "positive", "can delete files"),
        )
        assert len(a) == 40 and len(b) == 1
        if n == 2:
            return dict(
                evidence_ids=a[:1] + b,
                bindings=[
                    dict(requirement_id="r1", evidence_ids=a[:1]),
                    dict(requirement_id="r2", evidence_ids=b),
                ],
                analyses=[],
            )
        rows = [proposal("r1", "opportunity"), proposal("r2", "risk")]
        submitted.append((rows, deepcopy(rows)))
        return dict(evidence_ids=a[1 : 1 + diagnostic_count], bindings=[], analyses=rows)

    calls = text_sources(
        monkeypatch,
        response,
        [dict(id="positive", url=URL, title="OneDrive iOS", content=capability_body(40))],
    )
    db, run = run_case(SessionLocal, q)
    try:
        assert len(calls) == 3
        assert run.status == ("completed" if expected else "partial_failed"), run.error_message
        rows = list(db.scalars(select(ResearchFinding).where(ResearchFinding.run_id == run.id)))
        assert len([r for r in rows if r.claim_kind == "fact"]) == 2
        assert len([r for r in rows if r.claim_kind == "recommendation"]) == expected
        assert all(a == b for a, b in submitted)
        event = [e for e in run.execution_trace["events"] if e.get("stage") == "llm"][-1]
        codes = [r["code"] for r in event.get("analysis_rejections", [])]
        if not expected:
            assert codes.count("analysis_selection_budget_exhausted") == 2
    finally:
        db.close()


def test_duplicate_covered_facet_cannot_starve_a_new_requested_facet(
    SessionLocal, fake_settings, monkeypatch
):
    q = "How does OneDrive iPhone manually upload photos? Give opportunities and risks for the upload scenario."

    def response(m, n):
        if n == 1:
            p = plan(q)
            p["requirements"][0]["anchor_ids"] = ["q1"]
            p["requirements"].append(
                p["requirements"][0]
                | dict(
                    id="a1",
                    anchor_ids=["q2"],
                    kind="analysis",
                    analysis_facets=["opportunity", "risk"],
                )
            )
            return p
        ids = [e["evidence_id"] for e in m["evidence_catalog"]]
        c = dict(
            requirement_id="a1",
            facet="opportunity",
            operator="proposed_test",
            premises=[dict(requirement_id="r1", evidence_ids=[])],
            scenario="upload scenario",
            proposal="Test the documented upload.",
        )
        candidates = (
            [c | dict(proposal=f"Test documented upload variant {i}.") for i in range(8)]
            if n == 2
            else [c | dict(facet="risk")]
        )
        return dict(
            evidence_ids=ids,
            bindings=[dict(requirement_id="r1", evidence_ids=ids)],
            analyses=candidates,
        )

    calls, _, _ = install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        assert len(calls) == 3
        assert run.status == "completed", run.error_message
        rows = list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.claim_kind == "recommendation"
                )
            )
        )
        assert len(rows) <= 8
        assert {r.evidence_location["facet"] for r in rows} == {"opportunity", "risk"}
    finally:
        db.close()


@pytest.mark.parametrize("retained_candidate", [False, True])
def test_candidate_budget_uses_current_ledger_and_rechecks_history(
    SessionLocal, fake_settings, monkeypatch, retained_candidate
):
    q = two_fact_question()

    def response(m, n):
        if n == 1:
            return two_fact_plan(q)
        a, b = (
            ids_for(m, "positive", "can upload photos"),
            ids_for(m, "positive", "can delete files"),
        )
        assert len(a) == 40 and len(b) == 1
        if n == 2:
            return dict(
                evidence_ids=(a[:1] if retained_candidate else a[:39]) + b,
                bindings=[
                    dict(requirement_id="r1", evidence_ids=a[:1]),
                    dict(requirement_id="r2", evidence_ids=b),
                ],
                analyses=[proposal("r1", "opportunity")] if retained_candidate else [],
            )
        return dict(
            evidence_ids=a[1:] if retained_candidate else a[39:],
            bindings=[],
            analyses=[]
            if retained_candidate
            else [proposal("r1", "opportunity"), proposal("r2", "risk")],
        )

    text_sources(
        monkeypatch,
        response,
        [dict(id="positive", url=URL, title="OneDrive iOS", content=capability_body(40))],
    )
    db, run = run_case(SessionLocal, q)
    try:
        assert run.status == ("partial_failed" if retained_candidate else "completed"), (
            run.error_message
        )
        rows = list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.claim_kind == "recommendation"
                )
            )
        )
        assert len(rows) == (0 if retained_candidate else 2)
        if retained_candidate:
            event = [e for e in run.execution_trace["events"] if e.get("stage") == "llm"][-1]
            assert "analysis_selection_budget_exhausted" in [
                r["code"] for r in event["analysis_rejections"]
            ]
    finally:
        db.close()


@pytest.mark.parametrize("malformation", ["extra", "missing"])
def test_policy_reclassification_cannot_launder_invalid_original_schema(malformation):
    from app.services.research_pipeline_v7 import _pre_validate_planning
    from app.services.research_spec_v7 import SpecError, compile_spec

    q = "Using only official Microsoft Support, " + Q
    p = plan(q)
    policy = p["requirements"][0] | dict(
        id="policy",
        kind="condition",
        action="cite",
        object="official documentation",
        condition_quotes=["Using only official Microsoft Support"],
    )
    if malformation == "extra":
        policy["model_complete"] = True
    else:
        del policy["platform"]
    p["requirements"].append(policy)
    with pytest.raises(SpecError):
        revised, _ = _pre_validate_planning(q, p)
        compile_spec(q, revised)


def test_valid_pure_policy_query_is_removed_with_its_reclassified_requirement():
    from app.services.research_pipeline_v7 import _pre_validate_planning
    from app.services.research_spec_v7 import compile_spec

    q = "Using only official Microsoft Support, " + Q
    p = plan(q)
    p["requirements"].append(
        p["requirements"][0]
        | dict(
            id="policy",
            kind="condition",
            action="cite",
            object="official documentation",
            condition_quotes=["Using only official Microsoft Support"],
        )
    )
    p["queries"].append(dict(id="policy", text="Microsoft official support"))
    revised, event = _pre_validate_planning(q, p)
    assert event and event["removed_ids"] == ["policy"]
    assert all(r["id"] != "policy" for r in revised["requirements"])
    assert all(r["id"] != "policy" for r in revised["queries"])
    assert compile_spec(q, revised)["requirements"]
    assert len(p["requirements"]) == 2 and len(p["queries"]) == 2


def test_pure_policy_in_long_scope_has_the_same_policy_role():
    from app.services.research_pipeline_v7 import _pre_validate_planning

    policy = "Using only official Microsoft Support"
    scope = policy + ". " + "Mobile devices. " * 40
    p = plan(Q)
    p["requirements"].append(
        p["requirements"][0]
        | dict(
            id="policy",
            kind="condition",
            action="cite",
            object="official documentation",
            condition_quotes=[policy],
        )
    )
    revised, event = _pre_validate_planning(Q, p, scope)
    assert event and event["removed_ids"] == ["policy"]
    assert all(r["id"] != "policy" for r in revised["requirements"])


def one_analysis_plan(q, count=1):
    p = plan(q)
    r = p["requirements"][0] | dict(kind="capability", anchor_ids=["q1"], mode="unspecified")
    p["requirements"] = [r] + [
        r
        | dict(
            id=f"a{i}", kind="analysis", anchor_ids=["q2"], analysis_facets=["opportunity", "risk"]
        )
        for i in range(1, count + 1)
    ]
    return p


@pytest.mark.parametrize("positive_count", [39, 40])
def test_budget_cannot_restore_a_disproved_premise_and_its_old_analysis(
    SessionLocal, fake_settings, monkeypatch, positive_count
):
    q = "Can OneDrive iPhone upload photos? Give opportunities and risks for the upload scenario."
    body = (
        "# OneDrive iOS\n## Upload photos\n"
        + "".join(
            f"OneDrive on iPhone can upload photos using option {i}.\n"
            for i in range(positive_count)
        )
        + "OneDrive on iPhone cannot upload photos.\n"
    )

    def response(m, n):
        if n == 1:
            return one_analysis_plan(q)
        pos, neg = (
            ids_for(m, "positive", "can upload photos"),
            ids_for(m, "positive", "cannot upload photos"),
        )
        assert len(pos) == positive_count and len(neg) == 1
        c = dict(
            requirement_id="a1",
            facet="opportunity",
            operator="proposed_test",
            premises=[dict(requirement_id="r1", evidence_ids=[])],
            scenario="upload scenario",
            proposal="Test the documented upload.",
        )
        ids = pos if n == 2 else neg
        return dict(
            evidence_ids=ids,
            bindings=[dict(requirement_id="r1", evidence_ids=ids)],
            analyses=[c] if n == 2 else [],
        )

    calls = text_sources(
        monkeypatch, response, [dict(id="positive", url=URL, title="OneDrive iOS", content=body)]
    )
    db, run = run_case(SessionLocal, q)
    try:
        assert len(calls) == 3 and run.status == "partial_failed"
        r = next(i for i in run.coverage_report["items"] if i["requirement_id"] == "r1")
        assert r["status"] != "covered" and r["answer_polarity"] == "conflicting"
        assert not list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.claim_kind == "recommendation"
                )
            )
        )
        assert len({eid for i in run.coverage_report["items"] for eid in i["evidence_ids"]}) <= 40
        if positive_count == 40:
            assert "ledger_selection_budget_exhausted" in r["reason_codes"]
            assert r["conflict_groups"] and not r["evidence_ids"]
    finally:
        db.close()


@pytest.mark.parametrize("analysis_count", [4, 5])
def test_cross_round_facet_representatives_never_exceed_eight_analyses(
    SessionLocal, fake_settings, monkeypatch, analysis_count
):
    q = "Can OneDrive iPhone upload photos? Give opportunities and risks for the upload scenario."

    def response(m, n):
        if n == 1:
            return one_analysis_plan(q, analysis_count)
        ids = [e["evidence_id"] for e in m["evidence_catalog"]]
        rows = [
            dict(
                requirement_id=f"a{i}",
                facet=facet,
                operator="proposed_test",
                premises=[dict(requirement_id="r1", evidence_ids=[])],
                scenario="upload scenario",
                proposal="Test the documented upload.",
            )
            for i in (range(1, 5) if n == 2 else range(5, analysis_count + 1))
            for facet in ["opportunity", "risk"]
        ]
        return dict(
            evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)], analyses=rows
        )

    calls, _, _ = install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        assert len(calls) == (2 if analysis_count == 4 else 3)
        assert run.status == ("completed" if analysis_count == 4 else "partial_failed")
        rows = list(
            db.scalars(
                select(ResearchFinding).where(
                    ResearchFinding.run_id == run.id, ResearchFinding.claim_kind == "recommendation"
                )
            )
        )
        assert len(rows) == 8
        assert len(run.coverage_report["answer"]["analyses"]) == 8
    finally:
        db.close()


def test_analysis_rejections_and_adoption_keep_original_attempt_and_slot(
    SessionLocal, fake_settings, monkeypatch
):
    q = "Can OneDrive iPhone upload photos? Give opportunities and risks for the upload scenario."

    def response(m, n):
        if n == 1:
            return one_analysis_plan(q)
        ids = [e["evidence_id"] for e in m["evidence_catalog"]]
        c = dict(
            requirement_id="a1",
            facet="opportunity",
            operator="proposed_test",
            premises=[dict(requirement_id="r1", evidence_ids=[])],
            scenario="upload scenario",
            proposal="Test the documented upload.",
        )
        cs = (
            [c | dict(scenario="outside scenario one"), c | dict(scenario="outside scenario two")]
            if n == 2
            else [c]
        )
        return dict(
            evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)], analyses=cs
        )

    install(monkeypatch, response)
    db, run = run_case(SessionLocal, q)
    try:
        event = next(
            e
            for e in run.execution_trace["events"]
            if e.get("stage") == "llm" and e["attempt"] == 2
        )
        rejected = [r for r in event["analysis_rejections"] if r["code"] == "scenario_not_in_input"]
        assert len(rejected) == 2
        assert {(r["proposed_attempt"], r["proposed_slot"]) for r in rejected} == {(2, 0), (2, 1)}
        assert len({r["candidate_id"] for r in rejected}) == 2
        row = db.scalar(
            select(ResearchFinding).where(
                ResearchFinding.run_id == run.id, ResearchFinding.claim_kind == "recommendation"
            )
        )
        assert row is not None
        meta = row.evidence_location
        assert (meta["proposed_attempt"], meta["proposed_slot"]) == (3, 0)
        assert meta["candidate_id"] not in {r["candidate_id"] for r in rejected}
    finally:
        db.close()
