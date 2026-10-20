"""Owner Job only; executor cannot create approvals or change bound action parameters."""

import os
from psycopg import sql
from app.database.session import engine


def provision():
    with engine.begin() as c:
        d = c.connection.driver_connection
        exists = d.execute("SELECT 1 FROM pg_roles WHERE rolname='aiops_executor'").fetchone()
        d.execute(
            sql.SQL(
                ("ALTER" if exists else "CREATE")
                + " ROLE aiops_executor LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
            ).format(sql.Literal(os.environ["AIOPS_EXECUTOR_PASSWORD"]))
        )
        d.execute("GRANT CONNECT ON DATABASE aiops_control TO aiops_executor")
        d.execute("GRANT USAGE ON SCHEMA public TO aiops_executor")
        # V2 policy reads the persisted workflow kind and tool reservation timeline.
        # Executor still cannot write diagnoses, evidence, approvals or run state.
        d.execute(
            "GRANT SELECT ON aiops_actions, aiops_approvals, aiops_diagnoses, aiops_evidence, incidents, diagnosis_runs, aiops_timeline TO aiops_executor"
        )
        d.execute(
            "GRANT UPDATE (execution_status,started_at,finished_at,result_json) ON aiops_actions TO aiops_executor"
        )
        d.execute("ALTER ROLE aiops_executor SET statement_timeout TO '3s'")


if __name__ == "__main__":
    try:
        provision()
        print("Scoped executor role configured")
    except Exception as exc:
        print("Executor provisioning failed: " + type(exc).__name__)
        raise SystemExit(1)
