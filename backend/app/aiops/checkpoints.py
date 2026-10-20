"""Official PostgresSaver: owner setup once, runtime only DML."""

from contextlib import contextmanager
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy.engine import make_url
from app.core.config import get_settings


@contextmanager
def saver():
    url = (
        make_url(get_settings().database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    with PostgresSaver.from_conn_string(url) as value:
        yield value


if __name__ == "__main__":
    try:
        with saver() as value:
            value.setup()
        print("Official Postgres checkpoint schema initialized")
    except Exception as exc:
        print("Checkpoint setup failed: " + type(exc).__name__)
        raise SystemExit(1)
