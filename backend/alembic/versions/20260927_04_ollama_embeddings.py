"""store EmbeddingGemma vectors for runbook chunks

Revision ID: 20260927_04
Revises: 20260927_03
"""

from alembic import op
import sqlalchemy as sa

revision = "20260927_04"
down_revision = "20260927_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kb_chunks", sa.Column("embedding", sa.Text(), nullable=True))
    op.add_column("kb_chunks", sa.Column("embedding_model", sa.String(128), nullable=True))
    op.add_column("kb_chunks", sa.Column("embedding_dim", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_chunks", "embedding_dim")
    op.drop_column("kb_chunks", "embedding_model")
    op.drop_column("kb_chunks", "embedding")
