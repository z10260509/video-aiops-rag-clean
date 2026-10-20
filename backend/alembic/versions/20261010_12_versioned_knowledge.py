"""Versioned KB, optional pgvector capability for the dedicated AIOps DB only."""

from alembic import op
import sqlalchemy as sa
from app.core.config import get_settings

revision = "20261010_12"
down_revision = "20261010_11"
branch_labels = depends_on = None


def upgrade():
    for name, type_, default in [
        ("version", sa.String(64), "legacy"),
        ("kind", sa.String(32), "runbook"),
        ("status", sa.String(16), "active"),
        ("metadata_json", sa.Text(), "{}"),
    ]:
        op.add_column(
            "kb_documents", sa.Column(name, type_, nullable=False, server_default=default)
        )
    for name, type_, default in [
        ("ordinal", sa.Integer(), "0"),
        ("content_hash", sa.String(64), ""),
        ("document_version", sa.String(64), "legacy"),
        ("active", sa.Boolean(), sa.true()),
        ("metadata_json", sa.Text(), "{}"),
    ]:
        op.add_column("kb_chunks", sa.Column(name, type_, nullable=False, server_default=default))
    if get_settings().knowledge_vectors_enabled:
        # Fail if explicitly enabled without the extension files; never fake PG vectors.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("""CREATE TABLE aiops_kb_vectors (
            chunk_id varchar(255) NOT NULL REFERENCES kb_chunks(id) ON DELETE CASCADE,
            model_id varchar(128) NOT NULL, dimension integer NOT NULL CHECK(dimension BETWEEN 1 AND 2000),
            embedding vector NOT NULL, PRIMARY KEY(chunk_id,model_id),
            CHECK(vector_dims(embedding)=dimension))""")


def downgrade():
    # Capability table may not exist on legacy video databases.
    op.execute("DROP TABLE IF EXISTS aiops_kb_vectors")
    for name in ["metadata_json", "active", "document_version", "content_hash", "ordinal"]:
        op.drop_column("kb_chunks", name)
    for name in ["metadata_json", "status", "kind", "version"]:
        op.drop_column("kb_documents", name)
