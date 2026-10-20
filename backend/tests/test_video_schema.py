"""Video initialization must never create or rewrite AIOps control schema."""

import pytest
from sqlalchemy import create_engine, inspect, text
from app.services.provision_video_schema import provision


def test_fresh_video_schema_contains_only_video_tables_and_preserves_jobs(tmp_path):
    db = create_engine("sqlite:///" + str(tmp_path / "video.db"))
    provision(db)
    assert set(inspect(db).get_table_names()) == {
        "jobs",
        "video_task_outcomes",
        "video_job_executions",
        "video_job_repairs",
    }
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO jobs (id,original_filename,input_path,operation,video_codec,compression_preset,status,progress,created_at) VALUES ('original','original.mp4','/retained','transcode','h264','balanced','COMPLETED',100,CURRENT_TIMESTAMP)"
            )
        )
    provision(db)
    with db.connect() as c:
        assert (
            c.execute(text("SELECT status FROM jobs WHERE id='original'")).scalar() == "COMPLETED"
        )
    db.dispose()


def test_old_incomplete_job_schema_is_not_silently_rewritten(tmp_path):
    db = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    with db.begin() as c:
        c.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
    with pytest.raises(ValueError, match="reviewed migration"):
        provision(db)
    assert [c["name"] for c in inspect(db).get_columns("jobs")] == ["id"]
    db.dispose()
