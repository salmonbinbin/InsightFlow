"""Preserve populated 0006 data across pgvector conversion and reject invalid legacy vectors."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pgserver
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "work" / "phase2-acceptance"


def main():
    with tempfile.TemporaryDirectory(prefix="insightflow-phase2-migration-") as tmp:
        server = pgserver.get_server(Path(tmp) / "pg", cleanup_mode="stop")
        uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        env = {**os.environ, "DATABASE_URL": uri}
        engine = create_engine(uri)
        report = {}
        try:
            with (OUT / "populated-migration.log").open("w") as log:

                def migrate(direction, revision, success=True):
                    result = subprocess.run(
                        [sys.executable, "-m", "alembic", direction, revision],
                        cwd=ROOT,
                        env=env,
                        stdout=log,
                        stderr=log,
                    )
                    assert (result.returncode == 0) == success

                migrate("upgrade", "0006")
                pid, fid, rid, aid, eid = [uuid4() for _ in range(5)]
                ids = dict(pid=pid, fid=fid, rid=rid, aid=aid, eid=eid)
                with engine.begin() as db:
                    db.execute(
                        text(
                            "INSERT INTO projects(id,name,product_name,created_at) VALUES(:pid,'QA','QA',now())"
                        ),
                        ids,
                    )
                    db.execute(
                        text(
                            "INSERT INTO feedbacks(id,project_id,external_id,content,created_at) VALUES(:fid,:pid,'migration-sentinel',' 原文\n不变 ',now())"
                        ),
                        ids,
                    )
                    db.execute(
                        text(
                            "INSERT INTO analysis_runs(id,project_id,run_type,model,prompt_version,status,input_count,processed_count,success_count,failed_count,retry_count,input_tokens,output_tokens,latency_ms,attempt_count,unknown_token_calls,created_at) VALUES(:rid,:pid,'classification','test','test','completed',1,1,1,0,0,0,0,0,0,0,now())"
                        ),
                        ids,
                    )
                    db.execute(
                        text(
                            "INSERT INTO feedback_analyses(id,feedback_id,run_id,feedback_type,module,intent,pain_point,sentiment,severity,confidence,created_at) VALUES(:aid,:fid,:rid,'bug','file_upload','upload','failed','negative',4,0.9,now())"
                        ),
                        ids,
                    )
                    db.execute(
                        text(
                            "INSERT INTO feedback_embeddings(id,feedback_analysis_id,run_id,embedding_model,embedding_dimension,embedding,created_at) VALUES(:eid,:aid,:rid,'qa-model',2,'[1,0]',now())"
                        ),
                        ids,
                    )

                def snapshot():
                    with engine.connect() as db:
                        return [
                            dict(r) for r in db.execute(text("SELECT * FROM feedbacks")).mappings()
                        ]

                before = snapshot()
                for direction, revision in [
                    ("upgrade", "head"),
                    ("downgrade", "0006"),
                    ("upgrade", "head"),
                ]:
                    migrate(direction, revision)
                    assert before == snapshot()
                    with engine.connect() as db:
                        vector = db.scalar(
                            text("SELECT embedding::text FROM feedback_embeddings WHERE id=:eid"),
                            ids,
                        )
                        assert json.loads(vector) == [1, 0]
                report["populated_roundtrip_raw_vector_preserved"] = True
                inspector = inspect(engine)
                report["tables"] = {
                    t: {
                        "foreign_keys": inspector.get_foreign_keys(t),
                        "unique": inspector.get_unique_constraints(t),
                        "checks": inspector.get_check_constraints(t),
                        "indexes": inspector.get_indexes(t),
                    }
                    for t in [
                        "feedback_embeddings",
                        "clusters",
                        "cluster_members",
                        "cluster_reviews",
                    ]
                }
                migrate("downgrade", "0006")
                with engine.begin() as db:
                    db.execute(
                        text("UPDATE feedback_embeddings SET embedding_dimension=3 WHERE id=:eid"),
                        ids,
                    )
                migrate("upgrade", "head", success=False)
                with engine.connect() as db:
                    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
                    assert (
                        db.scalar(
                            text(
                                "SELECT udt_name FROM information_schema.columns WHERE table_name='feedback_embeddings' AND column_name='embedding'"
                            )
                        )
                        == "json"
                    )
                assert before == snapshot()
                report["invalid_legacy_dimension_atomic_rejection"] = True
                with engine.begin() as db:
                    db.execute(
                        text("UPDATE feedback_embeddings SET embedding_dimension=2 WHERE id=:eid"),
                        ids,
                    )
                migrate("upgrade", "head")
            print("PHASE2_POPULATED_MIGRATION_PASSED")
        finally:
            engine.dispose()
            server.cleanup()
            (OUT / "populated-migration.json").write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
