"""独立验收服务：仅用新建临时 PostgreSQL，18081 真模型，18083 故障替身。
运行后等待停止文件；Ctrl+C 清理本脚本创建的服务。不得用于生产。
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pgserver
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "work" / "phase1-acceptance"
OUT.mkdir(parents=True, exist_ok=True)
CONTROL = OUT / "fault-control.json"
STOP = OUT / "stop-server"
FAKE_KEY = "ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL"


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        mode = json.loads(CONTROL.read_text())["mode"]
        items = json.loads(body["messages"][0]["content"])["feedback_items"]
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "ids": [i["feedback_id"] for i in items],
            "model": body["model"],
            "key_in_body": FAKE_KEY in json.dumps(body),
            "tool_choice": body.get("tool_choice"),
        }
        with (OUT / "fault-calls.jsonl").open("a") as fp:
            fp.write(json.dumps(record) + "\n")
        if mode == "hold":
            time.sleep(4)
        code = 200
        if mode == "echo":
            code, result = 401, {"error": "invalid credential " + FAKE_KEY}
        elif mode == "fail" and any("FAIL" in i["content"] for i in items):
            code, result = 503, {"error": "synthetic upstream unavailable"}
        elif mode == "malformed":
            result = []
        else:
            result = {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_classifications",
                        "input": {
                            "results": [
                                dict(
                                    feedback_id=i["feedback_id"],
                                    feedback_type="bug",
                                    module="login",
                                    intent="sign_in",
                                    pain_point="cannot sign in",
                                    sentiment="negative",
                                    severity=4,
                                    confidence=0.9,
                                )
                                for i in items
                            ]
                        },
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        data = json.dumps(result).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


def main():
    # 拒绝复用已占用端口，避免第二次启动覆盖仍运行的验收实例状态。
    for port in (18081, 18082, 18083):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    STOP.unlink(missing_ok=True)
    CONTROL.write_text(json.dumps({"mode": "fail"}))
    children = []
    with tempfile.TemporaryDirectory(prefix="insightflow-acceptance-") as tmp:
        server = pgserver.get_server(Path(tmp) / "postgres", cleanup_mode="stop")
        uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        env = {**os.environ, "DATABASE_URL": uri}
        try:
            with (OUT / "migration-preservation.log").open("w") as log:
                subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "0001"],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                )
                engine = create_engine(uri)
                pid, fid = uuid4(), uuid4()
                with engine.begin() as db:
                    db.execute(
                        text(
                            "INSERT INTO projects(id,name,product_name,created_at) VALUES (:id, :n, :n, now())"
                        ),
                        {"id": pid, "n": "Phase0 migration sentinel"},
                    )
                    db.execute(
                        text(
                            "INSERT INTO feedbacks(id,project_id,external_id,content,created_at) VALUES (:id,:pid,:eid,:content,now())"
                        ),
                        {"id": fid, "pid": pid, "eid": "F1", "content": " 原文,换行\n保留  "},
                    )

                def snapshot():
                    with engine.connect() as db:
                        return [
                            dict(r)
                            for r in db.execute(
                                text("SELECT * FROM feedbacks WHERE id=:id"), {"id": fid}
                            ).mappings()
                        ]

                before = snapshot()
                subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                )
                subprocess.run(
                    [sys.executable, "-m", "alembic", "current"],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                )
                subprocess.run(
                    [sys.executable, "-m", "alembic", "check"],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                )
                inspector = inspect(engine)
                evidence = {
                    "raw_preserved": before == snapshot(),
                    "raw_sha256": hashlib.sha256(
                        json.dumps(before, default=str, sort_keys=True).encode()
                    ).hexdigest(),
                    "tables": {
                        table: {
                            "columns": [c["name"] for c in inspector.get_columns(table)],
                            "checks": inspector.get_check_constraints(table),
                            "foreign_keys": inspector.get_foreign_keys(table),
                            "unique": inspector.get_unique_constraints(table),
                            "indexes": inspector.get_indexes(table),
                        }
                        for table in inspector.get_table_names()
                    },
                }
                with engine.connect() as db:
                    evidence["version"] = db.scalar(text("SELECT version()"))
                    evidence["revision"] = db.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                (OUT / "database-schema.json").write_text(
                    json.dumps(evidence, default=str, indent=2)
                )
                engine.dispose()
            mock = ThreadingHTTPServer(("127.0.0.1", 18082), Upstream)
            threading.Thread(target=mock.serve_forever, daemon=True).start()
            real_env = dict(env)
            for target, source in [
                ("LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                ("LLM_BASE_URL", "ANTHROPIC_BASE_URL"),
                ("LLM_MODEL", "ANTHROPIC_MODEL"),
            ]:
                if not real_env.get(target) and real_env.get(source):
                    real_env[target] = real_env[source]
            fake_env = {
                **env,
                "LLM_API_KEY": FAKE_KEY,
                "LLM_BASE_URL": "http://127.0.0.1:18082",
                "LLM_MODEL": "acceptance-fake-model",
                "CLASSIFICATION_BATCH_SIZE": "1",
                "CLASSIFICATION_MAX_RETRIES": "1",
            }

            def launch(port, child_env, filename):
                log = (OUT / filename).open("a")
                p = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=ROOT,
                    env=child_env,
                    stdout=log,
                    stderr=log,
                )
                children.append(p)
                return p

            real = launch(18081, real_env, "real-http-server.log")
            fake = launch(18083, fake_env, "fault-http-server.log")
            state = {
                "database_url": uri,
                "real_pid": real.pid,
                "fault_pid": fake.pid,
                "real_url": "http://127.0.0.1:18081",
                "fault_url": "http://127.0.0.1:18083",
                "restart_count": 0,
            }
            (OUT / "server-state.json").write_text(json.dumps(state, indent=2))
            print(
                "READY: 真模型 http://127.0.0.1:18081/docs ; 隔离故障服务 http://127.0.0.1:18083/docs",
                flush=True,
            )
            while not STOP.exists():
                if fake.poll() is not None:
                    fake = launch(18083, fake_env, "fault-http-server.log")
                    state.update(fault_pid=fake.pid, restart_count=state["restart_count"] + 1)
                    (OUT / "server-state.json").write_text(json.dumps(state, indent=2))
                time.sleep(0.2)
            mock.shutdown()
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
            server.cleanup()
            print("本次验收服务及临时数据库已停止。", flush=True)


if __name__ == "__main__":
    main()
