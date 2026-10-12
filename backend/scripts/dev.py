"""本机开发启动器：独立 PostgreSQL 数据目录，退出时停止子进程。"""

import os
import subprocess
import sys
from pathlib import Path

import pgserver

root = Path(__file__).resolve().parents[1]
data = root.parent / "work" / "local-postgres"
data.parent.mkdir(exist_ok=True)
server = pgserver.get_server(data, cleanup_mode="stop")
try:
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True
    )
    print("InsightFlow 正在启动：http://127.0.0.1:8000/docs；Ctrl+C 停止。", flush=True)
    child = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=root,
        env=env,
    )
    try:
        raise SystemExit(child.wait())
    except KeyboardInterrupt:
        child.terminate()
        child.wait(timeout=10)
finally:
    server.cleanup()
