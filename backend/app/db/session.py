from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)


def get_db():
    with Session(engine) as session:
        yield session
