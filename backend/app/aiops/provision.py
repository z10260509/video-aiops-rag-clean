"""Migration Job only: owner grants runtime DML, never a runtime superuser."""

import os
from psycopg import sql
from app.database.session import engine


def provision():
    password = os.environ["AIOPS_RUNTIME_PASSWORD"]
    with engine.begin() as connection:
        driver = connection.connection.driver_connection
        exists = driver.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", ("aiops_app",)
        ).fetchone()
        command = "ALTER" if exists else "CREATE"
        driver.execute(
            sql.SQL(
                command + " ROLE aiops_app LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
            ).format(sql.Literal(password))
        )
        driver.execute("GRANT CONNECT ON DATABASE aiops_control TO aiops_app")
        driver.execute("GRANT USAGE ON SCHEMA public TO aiops_app")
        driver.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aiops_app"
        )
        driver.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aiops_app")
        driver.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aiops_app"
        )
        driver.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO aiops_app"
        )


if __name__ == "__main__":
    try:
        provision()
        print("Runtime database privileges configured")
    except Exception as exc:
        print("Provisioning failed: " + type(exc).__name__)
        raise SystemExit(1)
