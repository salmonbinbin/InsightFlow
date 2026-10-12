"""独立临时 PostgreSQL 验收；即使 pytest 失败也完成迁移往返并保留证据。"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "work" / "phase1-acceptance"
OUT.mkdir(parents=True, exist_ok=True)
commands = [
    ("upgrade", ["-m", "alembic", "upgrade", "head"]),
    ("current", ["-m", "alembic", "current"]),
    ("baseline", ["-m", "pytest", "-q", "tests/test_api.py", "tests/test_analysis.py"]),
    ("full", ["-m", "pytest", "-q"]),
    ("downgrade", ["-m", "alembic", "downgrade", "base"]),
    ("reupgrade", ["-m", "alembic", "upgrade", "head"]),
    ("check", ["-m", "alembic", "check"]),
]
results = []
with tempfile.TemporaryDirectory(prefix="insightflow-acceptance-test-") as tmp:
    server = pgserver.get_server(Path(tmp) / "data", cleanup_mode="stop")
    try:
        uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        env = {**os.environ, "DATABASE_URL": uri, "TEST_DATABASE_URL": uri}
        for name, args in commands:
            started = datetime.now(timezone.utc).isoformat()
            p = subprocess.run(
                [sys.executable, *args],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = p.stdout
            for key in ["LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
                if os.environ.get(key):
                    output = output.replace(os.environ[key], "[REDACTED]")
            (OUT / f"pg-{name}.log").write_text(
                started + "\n" + " ".join(args) + "\n" + output + "\nexit_code=" + str(p.returncode)
            )
            results.append({"command": args, "exit_code": p.returncode, "started": started})
            print(name, p.returncode, output[-1800:], flush=True)
        (OUT / "pg-commands.json").write_text(json.dumps(results, indent=2))
    finally:
        server.cleanup()
sys.exit(1 if any(r["exit_code"] for r in results) else 0)
