"""仅检查本轮隔离验收库，savepoint 内尝试约束并回滚。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.models import AnalysisRun, FeedbackAnalysis  # noqa: E402

OUT = ROOT.parent / "work" / "phase1-acceptance"
state = json.loads((OUT / "server-state.json").read_text())
engine = create_engine(state["database_url"])
report = {}
with Session(engine) as db:
    row = db.scalar(select(FeedbackAnalysis))
    values = {
        k: getattr(row, k)
        for k in [
            "feedback_id",
            "run_id",
            "feedback_type",
            "module",
            "intent",
            "pain_point",
            "sentiment",
            "severity",
            "confidence",
        ]
    }
    for name, changes in [
        ("duplicate", {}),
        ("feedback_fk", {"feedback_id": uuid4()}),
        ("run_fk", {"run_id": uuid4()}),
        ("severity", {"run_id": uuid4(), "severity": 6}),
        ("confidence_nan", {"run_id": uuid4(), "confidence": float("nan")}),
    ]:
        # Duplicate/FK rejection is isolated from and cannot change committed rows.
        try:
            with db.begin_nested():
                db.add(FeedbackAnalysis(**{**values, **changes}))
                db.flush()
            report[name] = "accepted"
        except IntegrityError as exc:
            report[name] = {
                "rejected": True,
                "constraint": getattr(getattr(exc.orig, "diag", None), "constraint_name", None),
            }
    report["run_status_db_constraint"] = []
    run = db.scalar(select(AnalysisRun))
    with db.begin_nested() as savepoint:
        run.status = "unrecognised"
        db.flush()
        report["run_status_db_constraint"] = "arbitrary_status_accepted"
        savepoint.rollback()
    db.rollback()
engine.dispose()
# 缺配置的真实 HTTP 服务，只指向本轮隔离数据库。
env = {
    **os.environ,
    "DATABASE_URL": state["database_url"],
    "LLM_API_KEY": "",
    "LLM_BASE_URL": "",
    "LLM_MODEL": "",
}
with (OUT / "missing-config-http.log").open("w") as log:
    child = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18084"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=log,
    )
    try:
        with httpx.Client(base_url="http://127.0.0.1:18084") as client:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            p = client.post(
                "/projects", json={"name": "缺配置隔离验收", "product_name": "QA"}
            ).json()
            r = client.post(f"/projects/{p['id']}/analysis")
            report["missing_config_http"] = {"status": r.status_code, "response": r.json()}
    finally:
        child.terminate()
        child.wait(timeout=10)
(OUT / "database-constraint-checks.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2)
)
print(json.dumps(report, ensure_ascii=False, indent=2))
