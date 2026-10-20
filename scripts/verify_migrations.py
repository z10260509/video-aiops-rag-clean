#!/usr/bin/env python3
"""Verify current migrations/roles/checkpoints in disposable Docker PostgreSQL only."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.project_images import CONTROL, DATABASE

CODE = """
import subprocess,uuid,json
from sqlalchemy import create_engine,text,inspect
from app.core.config import get_settings
from app.aiops.checkpoints import saver
from app.aiops.provision import provision
from app.aiops.provision_executor import provision as executor
run=lambda *args:subprocess.run(args,cwd='/app/backend',check=True,capture_output=True)
run('alembic','upgrade','20261010_09')
e=create_engine(get_settings().database_url)
with e.begin() as c:
 c.execute(text("INSERT INTO jobs (id,original_filename,input_path,operation,status,progress,created_at) VALUES (:id,'fixture.mp4','/not-real','transcode','COMPLETED',100,CURRENT_TIMESTAMP)"),{'id':str(uuid.uuid4())})
run('alembic','upgrade','head')
with saver() as s:s.setup()
provision();executor()
with e.connect() as c:
 assert c.execute(text('SELECT count(*) FROM jobs')).scalar()==1
 assert c.execute(text("SELECT has_table_privilege('aiops_executor','diagnosis_runs','SELECT')")).scalar()
 assert not c.execute(text("SELECT has_table_privilege('aiops_executor','aiops_approvals','INSERT')")).scalar()
 assert c.execute(text("SELECT has_table_privilege('aiops_app','checkpoints','SELECT')")).scalar()
 assert c.execute(text("SELECT has_table_privilege('aiops_executor','aiops_actions','SELECT')")).scalar()
 assert not c.execute(text("SELECT has_column_privilege('aiops_executor','aiops_actions','params_json','UPDATE')")).scalar()
 assert c.execute(text("SELECT atttypid::regtype::text FROM pg_attribute WHERE attrelid='aiops_kb_vectors'::regclass AND attname='embedding'")).scalar()=='vector'
 assert 'aiops_dispatch_requests' in inspect(e).get_table_names()
# A separate video DB must have only video tables, and the control DB must reject that initializer.
from app.services.provision_video_schema import provision as video_schema
from sqlalchemy.engine import make_url
from sqlalchemy import create_engine
try:video_schema(e)
except ValueError:pass
else:raise AssertionError('Control DB accepted video initializer')
with e.connect().execution_options(isolation_level='AUTOCOMMIT') as c:c.execute(text('CREATE DATABASE video_platform'))
v=create_engine(make_url(get_settings().database_url).set(database='video_platform'))
video_schema(v)
assert set(inspect(v).get_table_names())=={'jobs','video_task_outcomes','video_job_executions','video_job_repairs'}
video_schema(v);v.dispose()
run('alembic','downgrade','20261010_09')
with e.connect() as c:assert c.execute(text('SELECT count(*) FROM jobs')).scalar()==1
print(json.dumps({'job_preserved':True,'head_upgrade_and_09_downgrade':True,'official_checkpoints':True,'scoped_roles':True,'pgvector':True,'historical_table_preserved_at_head':True,'video_and_control_schema_separated':True}))
e.dispose()
"""


def verify(output):
    output = Path(output)
    if output.exists():
        raise ValueError("Choose a new output path")
    suffix = uuid.uuid4().hex[:12]
    network = "v2-migration-" + suffix
    db = network + "-db"
    env = dict(
        os.environ,
        POSTGRES_USER="aiops_owner",
        POSTGRES_DB="aiops_control",
        POSTGRES_PASSWORD=secrets.token_hex(24),
        AIOPS_RUNTIME_PASSWORD=secrets.token_hex(24),
        AIOPS_EXECUTOR_PASSWORD=secrets.token_hex(24),
        KNOWLEDGE_VECTORS_ENABLED="true",
    )
    env["DATABASE_URL"] = (
        "postgresql+psycopg://aiops_owner:"
        + env["POSTGRES_PASSWORD"]
        + "@"
        + db
        + ":5432/aiops_control"
    )
    made_network = made_db = False

    def call(args):
        r = subprocess.run(args, env=env, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("Scratch migration operation failed; no live database used")
        return r.stdout

    try:
        call(["docker", "network", "create", network])
        made_network = True
        call(
            [
                "docker",
                "run",
                "-d",
                "--name",
                db,
                "--network",
                network,
                "-e",
                "POSTGRES_USER",
                "-e",
                "POSTGRES_PASSWORD",
                "-e",
                "POSTGRES_DB",
                DATABASE,
            ]
        )
        made_db = True
        for _ in range(60):
            if (
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        db,
                        "pg_isready",
                        "-U",
                        "aiops_owner",
                        "-d",
                        "aiops_control",
                    ],
                    capture_output=True,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("Scratch PostgreSQL did not become ready")
        result = json.loads(
            call(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    network,
                    "-e",
                    "DATABASE_URL",
                    "-e",
                    "KNOWLEDGE_VECTORS_ENABLED",
                    "-e",
                    "AIOPS_RUNTIME_PASSWORD",
                    "-e",
                    "AIOPS_EXECUTOR_PASSWORD",
                    "--entrypoint",
                    "python",
                    CONTROL,
                    "-c",
                    CODE,
                ]
            )
        )
        result.update(image=CONTROL, live_database_modified=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return result
    finally:
        if made_db:
            subprocess.run(["docker", "rm", "-f", db], check=True, capture_output=True)
        if made_network:
            subprocess.run(["docker", "network", "rm", network], check=True, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.output)
