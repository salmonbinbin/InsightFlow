"""在全新临时 PostgreSQL 中验证迁移及 API；不连接用户现有数据库。"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pgserver

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="insightflow-pg-") as tmp:
    server = pgserver.get_server(Path(tmp) / "data", cleanup_mode="stop")
    try:
        uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        env = {**os.environ, "DATABASE_URL": uri, "TEST_DATABASE_URL": uri}
        print(server.psql("SELECT version();"), flush=True)
        for args in [
            ["-m", "alembic", "upgrade", "head"],
            ["-m", "alembic", "check"],
            ["-m", "pytest", "-q"],
            ["-m", "alembic", "downgrade", "base"],
            ["-m", "alembic", "upgrade", "head"],
            ["-m", "alembic", "check"],
        ]:
            subprocess.run([sys.executable, *args], cwd=root, env=env, check=True)
        print("POSTGRES_VERIFICATION_PASSED", flush=True)
    finally:
        server.cleanup()
