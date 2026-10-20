"""Video DB schema only: never apply the control DB's Alembic head here."""

from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from app.database.session import engine
from app.models.job import Job


def provision(bind=None):
    bind = bind or engine
    if bind.dialect.name != "sqlite" and make_url(bind.url).database != "video_platform":
        raise ValueError("Video schema initializer requires the video_platform database")
    with bind.begin() as connection:
        Job.__table__.create(connection, checkfirst=True)
        # Existing installations must already have the full current Job contract.
        columns = {c["name"] for c in inspect(connection).get_columns("jobs")}
        if not set(Job.__table__.columns.keys()) <= columns:
            raise ValueError(
                "Existing jobs schema requires a reviewed migration; no automatic rewrite"
            )
        from app.models.observability import VideoTaskOutcome
        from app.models.video_integration import VideoExecution, VideoRepair

        for model in [VideoTaskOutcome, VideoExecution, VideoRepair]:
            model.__table__.create(connection, checkfirst=True)


if __name__ == "__main__":
    provision()
    print("Video Job, outcome, execution and repair schema ready; no control tables created")
