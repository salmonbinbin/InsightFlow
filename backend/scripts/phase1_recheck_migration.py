"""复检0002已存在的异常Run能否升级；只操作全新临时库。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pgserver
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "work/phase1-fix-20261012"
OUT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="insightflow-recheck-migration-") as tmp:
    server = pgserver.get_server(Path(tmp) / "data", cleanup_mode="stop")
    try:
        uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        env = {**os.environ, "DATABASE_URL": uri}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "0002"], cwd=ROOT, env=env, check=True
        )
        engine = create_engine(uri)
        with engine.begin() as db:
            pid, rid, overflow_rid = uuid4(), uuid4(), uuid4()
            db.execute(
                text(
                    "INSERT INTO projects(id,name,product_name,created_at) VALUES (:id,:name,:name,now())"
                ),
                {"id": pid, "name": "隔离迁移复检"},
            )
            db.execute(
                text(
                    "INSERT INTO analysis_runs(id,project_id,run_type,model,prompt_version,status,input_count,processed_count,success_count,failed_count,created_at) VALUES (:id,:pid,'classification','fake','feedback-classification-v1','running',1,0,1,0,now())"
                ),
                {"id": rid, "pid": pid},
            )
            db.execute(
                text(
                    "INSERT INTO analysis_runs(id,project_id,run_type,model,prompt_version,status,input_count,processed_count,success_count,failed_count,created_at) VALUES (:id,:pid,'classification','fake','feedback-classification-v1','running',1,2,2,0,now())"
                ),
                {"id": overflow_rid, "pid": pid},
            )
            before_feedback_id = uuid4()
            db.execute(
                text(
                    "INSERT INTO feedbacks(id,project_id,external_id,content,created_at) "
                    "VALUES (:id,:pid,'F-before','原文保持',now()-interval '1 minute')"
                ),
                {"id": before_feedback_id, "pid": pid},
            )
            db.execute(
                text(
                    "INSERT INTO feedback_analyses(id,feedback_id,run_id,feedback_type,"
                    "module,intent,pain_point,sentiment,severity,confidence,created_at) "
                    "VALUES (:id,:fid,:rid,'bug','login','sign_in','cannot sign in',"
                    "'negative',4,0.9,now())"
                ),
                {"id": uuid4(), "fid": before_feedback_id, "rid": rid},
            )
            db.execute(
                text(
                    "INSERT INTO feedbacks(id,project_id,external_id,content,created_at) "
                    "VALUES (:id,:pid,'F-after','后导入原文',now()+interval '1 minute')"
                ),
                {"id": uuid4(), "pid": pid},
            )
        p = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with engine.connect() as db:
            revision = db.scalar(text("SELECT version_num FROM alembic_version"))
            runs = [
                dict(row)
                for row in db.execute(
                    text(
                        "SELECT id::text AS id,input_count,processed_count,success_count,failed_count,"
                        "input_feedback_external_ids FROM analysis_runs "
                        "WHERE id IN (:rid,:overflow) ORDER BY success_count"
                    ),
                    {"rid": rid, "overflow": overflow_rid},
                ).mappings()
            ]
            raw = db.execute(
                text(
                    "SELECT external_id,content FROM feedbacks WHERE project_id=:pid ORDER BY external_id"
                ),
                {"pid": pid},
            ).all()
        engine.dispose()
        (OUT / "migration-existing-inconsistent.log").write_text(p.stdout)
        result = {
            "seed": "0002合法但由上轮并发缺陷产生的 input=1 processed=0 success=1 Run",
            "upgrade_exit_code": p.returncode,
            "revision_after": revision,
            "runs_after": runs,
            "raw_after": [list(row) for row in raw],
        }
        result["passed"] = (
            p.returncode == 0
            and revision == "0005"
            and {
                row["id"]: (
                    row["input_count"],
                    row["processed_count"],
                    row["success_count"],
                    row["failed_count"],
                    row["input_feedback_external_ids"],
                )
                for row in runs
            }
            == {
                str(rid): (1, 1, 1, 0, ["F-before"]),
                str(overflow_rid): (1, 0, 0, 0, ["F-before"]),
            }
            and [list(row) for row in raw] == [["F-after", "后导入原文"], ["F-before", "原文保持"]]
        )
        (OUT / "migration-existing-inconsistent.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result["passed"]:
            raise SystemExit(1)
    finally:
        server.cleanup()
