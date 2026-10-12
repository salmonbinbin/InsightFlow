"""Native pgvector on PostgreSQL; JSON is exclusively a SQLite test fallback."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator


class EmbeddingVector(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Vector() if dialect.name == "postgresql" else JSON())

    def process_result_value(self, value, dialect):
        return None if value is None else [float(x) for x in value]
