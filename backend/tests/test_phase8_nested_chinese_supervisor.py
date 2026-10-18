"""Independent Chinese scope and nested-branch completeness regressions."""

import pytest
from test_phase8_research_claims import facts_for, setup
from test_phase8_v6_supervisor import SessionLocal as _session_fixture
from test_phase8_v6_supervisor import engine as _engine_fixture
from test_phase8_v6_supervisor import fake_settings as _settings_fixture
from test_phase8_v6_supervisor import install, run_case, task

engine = _engine_fixture
SessionLocal = _session_fixture
fake_settings = _settings_fixture


@pytest.mark.parametrize(
    "question",
    [
        "仅引用 Dropbox 官方帮助：在手机应用中如何上传照片？",
        "仅引用 Dropbox 官方帮助：在安卓应用中如何上传照片？",
        "仅引用 Dropbox 官方帮助：在平板上如何上传照片？",
    ],
)
def test_chinese_phone_scope_rejects_explicit_computer_instruction(question):
    run, project, _, entries = setup(
        "# Dropbox\n## Upload\nOn a computer, use the Dropbox app to upload photos.\n"
    )
    facts = facts_for(entries, run, project, question)
    assert facts and all(f.status == "not_verified" for f in facts)


@pytest.mark.parametrize("branch", ["none", "first_sentence", "full"])
@pytest.mark.parametrize("chinese", [False, True])
def test_existing_picture_question_requires_complete_nested_branch(
    SessionLocal, fake_settings, monkeypatch, branch, chinese
):
    q = (
        "仅引用 Microsoft 官方支持：iPhone 用户如何在 OneDrive 中选择已有图片并完成手动上传？"
        if chinese
        else "According to official Microsoft Support, how does an iPhone user select existing pictures for a manual upload in OneDrive? Cite only official Microsoft Support."
    )
    anchor = (
        "iPhone 用户如何在 OneDrive 中选择已有图片并完成手动上传"
        if chinese
        else "how does an iPhone user select existing pictures for a manual upload in OneDrive"
    )
    body = (
        "# Manually upload files or photos to OneDrive in iOS\n"
        "## Upload files and folders using the OneDrive for iOS app\n"
        "1. In the OneDrive app, tap the add icon.\n"
        "2. Tap Upload a file or photo.\n"
        "- If you tap Choose Existing, links appear to the Photos app on your device. Browse to the pictures you want to upload, select them, then on the top bar, tap Done.\n"
    )
    data = {
        "sources": [
            dict(
                provider_source_id="m",
                url="https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
                title="OneDrive iOS manual upload",
                domain="support.microsoft.com",
                snippet=body,
            )
        ]
    }

    def output(m):
        steps = [u["evidence_id"] for u in m["evidence_catalog"] if u["step_index"]]
        extras = [
            u["evidence_id"]
            for u in m["evidence_catalog"]
            if ("Choose Existing" in u["text"] or "Browse to the pictures" in u["text"])
        ]
        chosen = steps + (
            [] if branch == "none" else extras[:1] if branch == "first_sentence" else extras
        )
        return dict(
            tasks=[task(q, anchor, "procedure", "OneDrive", "iPhone", "app", "manual_upload")],
            evidence_ids=chosen,
            task_bindings=[dict(task_id="t1", evidence_ids=chosen)],
            analyses=[],
        )

    install(monkeypatch, data, output)
    db, run = run_case(SessionLocal, q)
    try:
        assert (run.status == "completed") == (branch == "full"), run.coverage_report
    finally:
        db.close()


def test_chinese_phone_scope_rejects_computer_section_even_without_inline_device():
    run, project, _, entries = setup(
        "# Dropbox\n## Upload photos from your computer\n1. Open the upload dialog.\n"
    )
    facts = facts_for(entries, run, project, "仅引用 Dropbox 官方帮助：在手机应用中如何上传照片？")
    assert facts and all(f.status == "not_verified" for f in facts)


def test_phone_branch_remains_valid_under_mixed_device_parent():
    run, project, _, entries = setup(
        "# Dropbox computer and mobile\n## Upload photos from a mobile phone\n1. Open the upload dialog.\n"
    )
    facts = facts_for(entries, run, project, "仅引用 Dropbox 官方帮助：在手机应用中如何上传照片？")
    assert facts and all(f.status == "confirmed" for f in facts)
