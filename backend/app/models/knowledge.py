"""Versioned RAG vectors in pgvector; SQLite serialization is used by isolated tests."""

import json

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, UserDefinedType

from app.database.session import Base


class PGVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "VECTOR"


class VectorStorage(TypeDecorator):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(PGVector() if dialect.name == "postgresql" else Text())

    def process_bind_param(self, value, dialect):
        return json.dumps(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return json.loads(value) if isinstance(value, str) else value


class KBVector(Base):
    __tablename__ = "aiops_kb_vectors"
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("kb_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list] = mapped_column(VectorStorage(), nullable=False)
