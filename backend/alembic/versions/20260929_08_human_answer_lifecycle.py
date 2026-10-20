"""track evidence source, verification, active answers, and answer revisions

Revision ID: 20260929_08
Revises: 20260928_07
"""

from alembic import op
import sqlalchemy as sa
import uuid

revision = "20260929_08"
down_revision = "20260928_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "human_supplements",
        sa.Column("source", sa.String(512), nullable=False, server_default="operator"),
    )
    op.add_column(
        "human_supplements",
        sa.Column(
            "verification_status", sa.String(16), nullable=False, server_default="unverified"
        ),
    )
    op.add_column(
        "human_supplements",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("human_supplements", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.create_table(
        "human_supplement_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "supplement_id", sa.String(36), sa.ForeignKey("human_supplements.id"), nullable=False
        ),
        sa.Column("question", sa.String(500), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("verification_status", sa.String(16), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_human_supplement_revisions_supplement_id",
        "human_supplement_revisions",
        ["supplement_id"],
    )

    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, incident_id, question, answer, submitted_at FROM human_supplements "
                "ORDER BY submitted_at DESC, id DESC"
            )
        )
        .mappings()
        .all()
    )
    groups = {}
    for row in rows:
        answer = row["answer"].casefold()
        fictional_example = (
            ("14:05" in answer and "redis ping" in answer)
            or ("14:00" in answer and "redis" in answer and "重啟" in answer)
            or ("12 個任務" in row["answer"] or "12 tasks" in answer)
        )
        if fictional_example:
            bind.execute(
                sa.text(
                    "INSERT INTO human_supplement_revisions "
                    "(id, supplement_id, question, answer, source, verification_status, action, changed_at) "
                    "SELECT :revision_id, id, question, answer, 'operator', 'unverified', 'retract', CURRENT_TIMESTAMP "
                    "FROM human_supplements WHERE id=:id"
                ),
                {
                    "revision_id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"fictional-example:{row['id']}")
                    ),
                    "id": row["id"],
                },
            )
            bind.execute(
                sa.text(
                    "UPDATE human_supplements SET active=false, verification_status='retracted', "
                    "source='虛構示例，非系統觀測', updated_at=submitted_at WHERE id=:id"
                ),
                {"id": row["id"]},
            )
            continue
        key = (row["incident_id"], row["question"].strip().casefold())
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        meaningful = [
            row
            for row in group
            if row["answer"].strip().casefold() not in {"test", "testing", "測試"}
        ]
        keep = (meaningful or group)[0]
        for row in group:
            bind.execute(
                sa.text(
                    "UPDATE human_supplements SET active=:active, updated_at=submitted_at WHERE id=:id"
                ),
                {"active": row["id"] == keep["id"], "id": row["id"]},
            )
    op.alter_column("human_supplements", "updated_at", nullable=False)


def downgrade() -> None:
    op.drop_index(
        "ix_human_supplement_revisions_supplement_id", table_name="human_supplement_revisions"
    )
    op.drop_table("human_supplement_revisions")
    op.drop_column("human_supplements", "updated_at")
    op.drop_column("human_supplements", "active")
    op.drop_column("human_supplements", "verification_status")
    op.drop_column("human_supplements", "source")
