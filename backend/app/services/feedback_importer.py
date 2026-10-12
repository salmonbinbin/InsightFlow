from datetime import timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import Feedback


class ImportConflict(ValueError):
    pass


def comparable(value):
    if hasattr(value, "tzinfo"):
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
    return value


def import_feedback(db, project_id, rows):
    # ON CONFLICT plus the unique constraint also protects concurrent retries.
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    imported = 0
    try:
        for row in rows:
            statement = (
                insert(Feedback)
                .values(project_id=project_id, **row)
                .on_conflict_do_nothing(index_elements=["project_id", "external_id"])
                .returning(Feedback.id)
            )
            inserted_id = db.execute(statement).scalar_one_or_none()
            if inserted_id is not None:
                imported += 1
                continue
            existing = db.scalar(
                select(Feedback).where(
                    Feedback.project_id == project_id, Feedback.external_id == row["external_id"]
                )
            )
            if any(
                comparable(getattr(existing, key)) != comparable(value)
                for key, value in row.items()
            ):
                raise ImportConflict(f"反馈 {row['external_id']} 已存在且数据不同，整批未导入")
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"total": len(rows), "imported": imported, "skipped": len(rows) - imported}
