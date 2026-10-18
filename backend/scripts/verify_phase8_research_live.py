"""Phase 8 live acceptance capture; fresh PostgreSQL and synthetic internal inputs only.

This captures external retrieval/model/validation evidence, never a semantic PASS.
Invoke from backend with pgserver==0.1.4. Keys are read in this process only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

QUESTIONS = [
    (
        "Google Photos",
        "In the Google Photos Android app, can a user manually select several photos and start backup in one action? Cite only official Google Help; separate manual upload from automatic backup.",
    ),
    (
        "Dropbox",
        "In the Dropbox iPhone app, can a user select and upload multiple photos at once? Cite only official Dropbox Help and state any limits actually documented.",
    ),
    (
        "OneDrive",
        "In the OneDrive iPhone app, can a user manually select multiple photos for one upload? Cite only official Microsoft Support; separate manual upload from camera upload.",
    ),
    (
        "iCloud Photos",
        "On iPhone, distinguish the Photos app iCloud sync, iCloud.com upload, and Shared Photo Library. Which of these supports selecting multiple photos for a manual upload? Cite only official Apple Support.",
    ),
    (
        "Google Drive",
        "In the Google Drive Android app, can a user select multiple device photos and upload them in one action? Cite only official Google Drive Help; if official documentation is absent, mark not verified.",
    ),
    (
        "Google Photos",
        "According to official Google Photos Help, what are the steps to manually back up one photo or video in the Android app? Cite only official Google Help; distinguish automatic backup.",
    ),
    (
        "Dropbox",
        "According to official Dropbox Help, can mobile users manually add photos when camera uploads are disabled? Cite only official Dropbox Help.",
    ),
    (
        "OneDrive",
        "According to official Microsoft Support, how does an iPhone user select existing pictures for a manual upload in OneDrive? Cite only official Microsoft Support.",
    ),
    (
        "iCloud Photos",
        "According to official Apple Support, which upload choices does iCloud.com show on a phone or tablet, and where should users upload to a Shared Photo Library? Cite only official Apple Support.",
    ),
    (
        "Google Drive",
        "According to official Google Drive Help, what are the Android app steps to upload files? Cite only official Google Drive Help.",
    ),
]


EXTENDED_QUESTIONS = [
    (
        "中文：Google Photos",
        "请仅根据 Google 官方帮助，说明 Google Photos 安卓应用中手动备份一张照片或视频的完整步骤，并区分自动备份。",
    ),
    (
        "中文：Dropbox",
        "仅引用 Dropbox 官方帮助：关闭相机上传（camera uploads）后，移动端还能手动添加照片吗？请保留完整操作步骤和禁用条件。",
    ),
    (
        "中文：iCloud",
        "仅引用 Apple 官方支持：iCloud.com 在手机或平板上提供哪些上传选项？如果要上传到共享照片图库，应使用哪个入口？请保留版本条件。",
    ),
    (
        "英文改写：Drive",
        "Using only official Google Drive Help, explain how an Android user uploads files from the app and locates the uploaded files.",
    ),
    (
        "中文：OneDrive",
        "仅引用 Microsoft 官方支持：iPhone 用户如何在 OneDrive 中选择已有图片并完成手动上传？",
    ),
    (
        "英文分析：两产品",
        "Compare manual photo upload entry points in Google Photos Android and Dropbox mobile using only official help pages. Give sample product patterns, scenario-relative strengths and weaknesses, opportunities and risks. Distinguish facts, inferences and recommendations.",
    ),
    (
        "中文分析：两产品",
        "请只依据 Google Photos 和 Dropbox 的官方帮助，比较移动端手动添加照片的流程。给出仅限本次两款产品样本的模式、相对该场景的优缺点、机会和风险；清楚区分事实、推断与建议。",
    ),
    (
        "未知条件：离线",
        "In Google Photos Android, what are the steps to manually back up one photo while offline? Cite only official Google Help. Keep the offline condition; do not infer unsupported capability.",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, choices=(5, 10), default=10)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--suite", choices=("original", "extended"), default="original")
    parser.add_argument("--search-provider", choices=("tavily", "minimax"))
    parser.add_argument(
        "--only-case", type=int, help="Single original-index smoke, not full-suite acceptance"
    )
    parser.add_argument(
        "--prompt-version",
        choices=("research-agent-v6", "research-agent-v7"),
        default="research-agent-v6",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    from dotenv import dotenv_values

    for key, value in dotenv_values(root / ".env").items():
        if value and key not in os.environ:
            os.environ[key] = value
    if args.search_provider:
        os.environ["RESEARCH_SEARCH_PROVIDER"] = args.search_provider
    selected_provider = (
        os.environ.get("RESEARCH_SEARCH_PROVIDER", "tavily")
        if args.prompt_version == "research-agent-v7"
        else "tavily"
    )
    required_key = "MINIMAX_SEARCH_API_KEY" if selected_provider == "minimax" else "TAVILY_API_KEY"
    if not os.environ.get(required_key):
        print("REAL_RESEARCH_NOT_MEASURED: selected search provider configuration missing.")
        return 2
    config_path = root.parent / "key/.env"
    if not all(os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")):
        if config_path.exists():
            config = json.loads(config_path.read_text()).get("env", {})
            for dest, source in [
                ("LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                ("LLM_BASE_URL", "ANTHROPIC_BASE_URL"),
                ("LLM_MODEL", "ANTHROPIC_MODEL"),
            ]:
                if config.get(source) and not os.environ.get(dest):
                    os.environ[dest] = config[source]
    if not all(os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")):
        print("REAL_RESEARCH_NOT_MEASURED: configure LLM_API_KEY / LLM_BASE_URL / LLM_MODEL.")
        return 2
    if args.check_config:
        print("CONFIG_PRESENT_NOT_CALL_TESTED")
        return 0

    import pgserver

    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (
        root.parent
        / "work/phase8-supervision"
        / (stamp + "-" + args.prompt_version + "-" + args.suite)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "live-research.json"
    cases = (QUESTIONS if args.suite == "original" else EXTENDED_QUESTIONS)[: args.samples]
    if args.only_case is not None:
        if not 1 <= args.only_case <= len(cases):
            parser.error("only-case out of suite range")
        cases = [cases[args.only_case - 1]]
    report: dict = {
        "kind": "live independent HTTP search + LLM; synthetic internal seed",
        "search_provider": selected_provider,
        "single_case_smoke": args.only_case,
        "semantic_acceptance": "NOT_MEASURED_REQUIRES_SOURCE_REVIEW",
        "samples": [],
        "sample_set": args.suite,
        "requested_prompt_version": args.prompt_version,
        "planned_sample_count": len(cases),
    }
    secrets = [
        os.environ[k]
        for k in ("TAVILY_API_KEY", "MINIMAX_SEARCH_API_KEY", "LLM_API_KEY")
        if os.environ.get(k)
    ]

    def save() -> None:
        serialized = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        for secret in secrets:
            serialized = serialized.replace(secret, "[REDACTED]")
        output.write_text(serialized)

    with tempfile.TemporaryDirectory(prefix="insightflow-p8-live-") as temporary:
        server = pgserver.get_server(Path(temporary) / "data", cleanup_mode="stop")
        try:
            uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
            os.environ["DATABASE_URL"] = uri
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root,
                env=os.environ,
                check=True,
                capture_output=True,
            )
            from sqlalchemy import create_engine, select
            from sqlalchemy.orm import Session
            from test_phase8_research import _make_project, _seed_minimal_pipeline

            from app.models import Feedback, ResearchFinding, ResearchSource
            from app.services import research_service as service

            real_search = service.ResearchProviderClient.search
            real_generate = service.ResearchLLMClient.generate_research
            real_validate = service.validate_research_payload
            audit: list = []

            def capture_search(client, query):
                result = real_search(client, query)
                audit.append(
                    {
                        "stage": "provider_before_filters",
                        "query": query,
                        "sources": [asdict(s) for s in result.sources],
                    }
                )
                return result

            def capture_generate(client, **kwargs):
                record = {
                    "stage": "llm",
                    "model_phase": kwargs["user_message"].get("stage"),
                    "question_anchors": kwargs["user_message"].get("question_anchors"),
                    "sources_after_filters": kwargs["user_message"]["search_results"],
                    "retry_hint": kwargs.get("retry_hint"),
                    "task_contract": kwargs["user_message"].get("task_contract"),
                    "evidence_catalog": kwargs["user_message"].get("evidence_catalog"),
                }
                audit.append(record)
                try:
                    result = real_generate(client, **kwargs)
                    record["raw_structured_payload"] = result.payload
                    return result
                except Exception as error:
                    record["error_type"] = type(error).__name__
                    record["response_metadata"] = getattr(error, "response_metadata", None)
                    raise

            def capture_validate(payload, **kwargs):
                record = {"stage": "validator"}
                audit.append(record)
                try:
                    result = real_validate(payload, **kwargs)
                    record["validated_findings"] = [asdict(f) for f in result.findings]
                    return result
                except service.ResearchValidationError as error:
                    record["error_code"] = error.code
                    raise

            service.ResearchProviderClient.search = capture_search
            service.ResearchLLMClient.generate_research = capture_generate
            service.validate_research_payload = capture_validate
            engine = create_engine(uri)
            try:
                with Session(engine) as database:
                    project = _make_project(database, name="Phase8 live acceptance synthetic seed")
                    seed = _seed_minimal_pipeline(database, project=project)

                    def raw_snapshot():
                        database.expire_all()
                        return [
                            {
                                column.name: str(getattr(row, column.name))
                                for column in Feedback.__table__.columns
                            }
                            for row in database.scalars(select(Feedback).order_by(Feedback.id))
                        ]

                    raw_before = raw_snapshot()
                    for product, question in cases:
                        print("RESEARCH_CASE_STARTED", product, flush=True)
                        audit = []
                        run = service.start_research_run(
                            database,
                            project.id,
                            seed["insight_id"],
                            research_question=question,
                            scope="Mobile photo upload; prefer official product support pages",
                            prompt_version=args.prompt_version,
                        )
                        print("RESEARCH_CASE_FINISHED", product, run.status, flush=True)
                        sources = database.scalars(
                            select(ResearchSource).where(ResearchSource.run_id == run.id)
                        ).all()
                        findings = database.scalars(
                            select(ResearchFinding).where(ResearchFinding.run_id == run.id)
                        ).all()
                        sample = {
                            key: getattr(run, key)
                            for key in (
                                "status",
                                "prompt_version",
                                "provider_query",
                                "model",
                                "attempt_count",
                                "retry_count",
                                "latency_ms",
                                "input_tokens",
                                "output_tokens",
                                "error_message",
                                "task_contract",
                                "coverage_report",
                                "execution_trace",
                                "unknown_token_calls",
                            )
                        }
                        sample.update(
                            {
                                "product": product,
                                "question": question,
                                "run_id": str(run.id),
                                "audit": audit,
                                "sources": [
                                    {
                                        key: getattr(s, key)
                                        for key in (
                                            "id",
                                            "run_id",
                                            "retrieved_at",
                                            "provider_source_id",
                                            "url",
                                            "title",
                                            "domain",
                                            "snippet",
                                        )
                                    }
                                    for s in sources
                                ],
                                "findings": [
                                    {
                                        key: getattr(f, key)
                                        for key in (
                                            "id",
                                            "run_id",
                                            "claim_kind",
                                            "supporting_finding_ids",
                                            "evidence_location",
                                            "category",
                                            "status",
                                            "title",
                                            "description",
                                            "source_refs",
                                        )
                                    }
                                    for f in findings
                                ],
                                "human_source_review": "NOT_MEASURED",
                            }
                        )
                        report["samples"].append(sample)
                        save()
                        print(f"{product}: {run.status} run={run.id}", flush=True)
                    report["raw_all_fields_unchanged"] = raw_before == raw_snapshot()
                    report["raw_row_count"] = len(raw_before)
                    save()
            finally:
                engine.dispose()
                service.ResearchProviderClient.search = real_search
                service.ResearchLLMClient.generate_research = real_generate
                service.validate_research_payload = real_validate
        finally:
            server.cleanup()
    print(f"EVIDENCE: {output}")
    print("SOURCE_REVIEW_REQUIRED: completed is not semantic acceptance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
