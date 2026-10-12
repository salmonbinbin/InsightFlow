"""对 phase1_acceptance_server.py 的隔离服务执行真实 HTTP 故障验收。"""

import hashlib
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))
from app.models import AnalysisRun, AnalysisRunFailure  # noqa: E402

OUT = ROOT.parent / "work" / "phase1-acceptance"
STATE = OUT / "server-state.json"
CONTROL = OUT / "fault-control.json"
FAKE_KEY = "ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL"
state = json.loads(STATE.read_text())
engine = create_engine(state["database_url"])
report = {"requests": [], "observations": {}}
client = httpx.Client(base_url=state["fault_url"], timeout=30)


def mode(value):
    CONTROL.write_text(json.dumps({"mode": value}))


def request(method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    try:
        data = response.json()
    except ValueError:
        data = response.text[:200]
    summary = json.loads(json.dumps(data).replace(FAKE_KEY, "[FAKE_KEY_REDACTED]"))
    report["requests"].append(
        {"method": method, "path": path, "status": response.status_code, "response": summary}
    )
    return response, data


def create_project(content="feedback_id,content\nF1,正常反馈\nF2,FAIL 登录一直失败\n"):
    _, data = request("POST", "/projects", json={"name": "独立验收故障项目", "product_name": "QA"})
    pid = data["id"]
    request(
        "POST",
        f"/projects/{pid}/feedback/import",
        files={"file": ("qa.csv", content.encode(), "text/csv")},
    )
    return pid


try:
    obs = report["observations"]
    for path in ["/health", "/health/ready", "/openapi.json", "/docs"]:
        response = client.get(path)
        obs[path] = {"status": response.status_code, "bytes": len(response.content)}
    mode("fail")
    pid = create_project()
    _, raw_before = request("GET", f"/projects/{pid}/feedback")
    _, run = request("POST", f"/projects/{pid}/analysis")
    rid = run["id"]
    _, first_results = request("GET", f"/analysis/{rid}/results")
    mode("valid")
    _, retried = request("POST", f"/analysis/{rid}/retry")
    _, second_results = request("GET", f"/analysis/{rid}/results")
    request("POST", f"/analysis/{rid}/retry")
    _, raw_after = request("GET", f"/projects/{pid}/feedback")
    obs["partial_retry"] = {
        "project_id": pid,
        "run_id": rid,
        "before": run,
        "after": retried,
        "raw_unchanged": raw_before == raw_after,
        "successful_ids_preserved": set(i["id"] for i in first_results["items"]).issubset(
            i["id"] for i in second_results["items"]
        ),
    }
    with Session(engine) as db:
        stored = db.get(AnalysisRun, UUID(rid))
        obs["api_database_match"] = all(
            getattr(stored, k) == retried[k]
            for k in [
                "status",
                "input_count",
                "processed_count",
                "success_count",
                "failed_count",
                "input_tokens",
                "output_tokens",
                "latency_ms",
                "retry_count",
                "model",
                "prompt_version",
            ]
        )
        obs["retry_failures_remaining"] = len(
            db.scalars(
                select(AnalysisRunFailure).where(AnalysisRunFailure.run_id == UUID(rid))
            ).all()
        )
    for method, path in [
        ("GET", "/analysis/not-a-uuid"),
        ("GET", f"/analysis/{uuid4()}"),
        ("POST", f"/projects/{uuid4()}/analysis"),
        ("GET", f"/analysis/{rid}/results?limit=0"),
        ("GET", f"/analysis/{rid}/results?offset=-1"),
    ]:
        request(method, path)

    mode("echo")
    echo_pid = create_project("feedback_id,content\nF1,登录失败\n")
    _, echo_run = request("POST", f"/projects/{echo_pid}/analysis")
    with Session(engine) as db:
        failure = db.scalar(
            select(AnalysisRunFailure).where(AnalysisRunFailure.run_id == UUID(echo_run["id"]))
        )
        obs["fake_credential_persisted"] = FAKE_KEY in (failure.last_error or "")
        obs["failure_attempts"] = failure.attempts
    mode("malformed")
    malformed_pid = create_project("feedback_id,content\nF1,登录失败\n")
    _, malformed = request("POST", f"/projects/{malformed_pid}/analysis")
    _, malformed_retry = request("POST", f"/analysis/{malformed['id']}/retry")
    obs["malformed_protocol"] = {"first": malformed, "retry": malformed_retry}

    mode("fail")
    concurrent_pid = create_project("feedback_id,content\nF1,FAIL 登录失败\n")
    _, concurrent_run = request("POST", f"/projects/{concurrent_pid}/analysis")
    mode("hold")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: client.post(f"/analysis/{concurrent_run['id']}/retry"), range(2))
        )
    obs["concurrent_retry_http"] = [
        {"status": r.status_code, "response": r.text[:300]} for r in responses
    ]
    _, current = request("GET", f"/analysis/{concurrent_run['id']}")
    obs["concurrent_final"] = current

    # 真正终止本脚本专用故障服务，父脚本会重启；不触碰 8000 现有实例。
    mode("hold")
    interrupted_pid = create_project("feedback_id,content\nF1,登录失败\n")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/projects/{interrupted_pid}/analysis")
        deadline = time.monotonic() + 5
        interrupted = None
        while time.monotonic() < deadline:
            with Session(engine) as db:
                interrupted = db.scalar(
                    select(AnalysisRun).where(AnalysisRun.project_id == UUID(interrupted_pid))
                )
                if interrupted:
                    interrupted_id = str(interrupted.id)
                    break
            time.sleep(0.05)
        if interrupted is None:
            raise RuntimeError("run not created before interrupt")
        os.kill(json.loads(STATE.read_text())["fault_pid"], signal.SIGKILL)
        try:
            future.result()
        except httpx.HTTPError as exc:
            obs["interrupted_http_error"] = type(exc).__name__
    mode("valid")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    _, after_restart = request("GET", f"/analysis/{interrupted_id}")
    _, after_retry = request("POST", f"/analysis/{interrupted_id}/retry")
    obs["real_process_interruption"] = {
        "project_id": interrupted_pid,
        "run_id": interrupted_id,
        "after_restart": after_restart,
        "after_retry": after_retry,
    }
    # 留下一个可供用户在本次服务仍运行时手动复测的 partial_failed Run。
    mode("fail")
    manual_pid = create_project()
    _, manual = request("POST", f"/projects/{manual_pid}/analysis")
    obs["manual_retry_fixture"] = {
        "project_id": manual_pid,
        "run_id": manual["id"],
        "status": manual["status"],
        "restore_command": "将 fault-control.json 的 mode 改为 valid，再在 18083/docs 调用 retry",
    }
    with engine.connect() as db:
        obs["orphan_results"] = db.scalar(
            text(
                "SELECT count(*) FROM feedback_analyses a LEFT JOIN feedbacks f ON f.id=a.feedback_id LEFT JOIN analysis_runs r ON r.id=a.run_id WHERE f.id IS NULL OR r.id IS NULL"
            )
        )
        obs["cross_project_results"] = db.scalar(
            text(
                "SELECT count(*) FROM feedback_analyses a JOIN feedbacks f ON f.id=a.feedback_id JOIN analysis_runs r ON r.id=a.run_id WHERE f.project_id<>r.project_id"
            )
        )
    obs["raw_before_sha256"] = hashlib.sha256(
        json.dumps(raw_before, sort_keys=True).encode()
    ).hexdigest()
    obs["raw_after_sha256"] = hashlib.sha256(
        json.dumps(raw_after, sort_keys=True).encode()
    ).hexdigest()
finally:
    client.close()
    engine.dispose()
    (OUT / "http-fault-evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report["observations"], ensure_ascii=False, indent=2))
