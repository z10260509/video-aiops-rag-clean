from io import BytesIO

from fastapi import HTTPException, Request, UploadFile
from starlette.datastructures import Headers

from app.api.jobs import create_job, delete_job, get_job, get_thumbnail, list_jobs, play_video
from app.models.job import CompressionPreset, Job, JobStatus, VideoCodec
from app.models.job import Operation


def upload(filename: str, content: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        filename=filename, file=BytesIO(content), headers=Headers({"content-type": content_type})
    )


def test_create_and_list_job(db_and_settings, monkeypatch):
    Session, settings = db_and_settings
    monkeypatch.setattr("app.api.jobs.enqueue_job", lambda job_id: None)
    with Session() as db:
        created = create_job(
            upload("my-video.mp4", b"fake mp4", "video/mp4"),
            Operation.TRANSCODE,
            "720p",
            db,
            settings,
            VideoCodec.H264,
            CompressionPreset.BALANCED,
        )
        assert created.status == "QUEUED"
        assert len(list(settings.input_dir.iterdir())) == 1
        listed = list_jobs(db)
        assert listed[0].id == created.id
        assert listed[0].status == "QUEUED"


def test_rejects_non_mp4(db_and_settings):
    Session, settings = db_and_settings
    with Session() as db:
        try:
            create_job(
                upload("malware.exe", b"x", "application/octet-stream"),
                Operation.TRANSCODE,
                "720p",
                db,
                settings,
            )
        except HTTPException as exc:
            assert exc.status_code == 415
        else:
            raise AssertionError("non-MP4 upload was accepted")


def test_video_range_response(db_and_settings):
    Session, settings = db_and_settings
    settings.output_dir.mkdir(parents=True)
    video = settings.output_dir / "result.mp4"
    video.write_bytes(b"0123456789")
    with Session() as db:
        job = Job(
            original_filename="source.mp4",
            input_path="/tmp/source.mp4",
            output_path=str(video),
            operation="transcode",
            target_resolution="720p",
            status=JobStatus.COMPLETED.value,
            progress=100,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    request = Request({"type": "http", "method": "GET", "headers": [(b"range", b"bytes=2-5")]})
    with Session() as db:
        response = play_video(job_id, request, db)
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"


def test_job_retrieval(db_and_settings):
    Session, _ = db_and_settings
    with Session() as db:
        job = Job(
            original_filename="source.mp4",
            input_path="/tmp/source.mp4",
            operation="transcode",
            target_resolution="480p",
            status=JobStatus.QUEUED.value,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        assert get_job(job.id, db).id == job.id


def test_delete_job_removes_metadata_and_media(db_and_settings):
    Session, settings = db_and_settings
    settings.input_dir.mkdir(parents=True)
    settings.output_dir.mkdir(parents=True)
    input_file = settings.input_dir / "source.mp4"
    output_file = settings.output_dir / "result.mp4"
    input_file.write_bytes(b"source")
    output_file.write_bytes(b"result")
    with Session() as db:
        job = Job(
            original_filename="source.mp4",
            input_path=str(input_file),
            output_path=str(output_file),
            operation="transcode",
            target_resolution="720p",
            status=JobStatus.COMPLETED.value,
            progress=100,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        delete_job(job_id, db)
        assert db.get(Job, job_id) is None
    assert not input_file.exists()
    assert not output_file.exists()


def test_thumbnail_is_served_for_completed_job(db_and_settings):
    Session, settings = db_and_settings
    settings.thumbnail_dir.mkdir(parents=True)
    thumbnail = settings.thumbnail_dir / "thumbnail.jpg"
    thumbnail.write_bytes(b"jpeg")
    with Session() as db:
        job = Job(
            original_filename="source.mp4",
            input_path="/tmp/source.mp4",
            thumbnail_path=str(thumbnail),
            operation="transcode",
            target_resolution="720p",
            status=JobStatus.COMPLETED.value,
            progress=100,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        response = get_thumbnail(job.id, db)
    assert response.media_type == "image/jpeg"


def test_paginated_jobs_preserve_array_contract_and_stable_order(db_and_settings):
    from datetime import datetime
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database.session import get_db

    Session, _ = db_and_settings
    with Session() as db:
        for number in range(25):
            db.add(
                Job(
                    id=f"{number:036d}",
                    original_filename=f"{number}.mp4",
                    input_path="/not-real",
                    operation="transcode",
                    target_resolution="480p",
                    created_at=datetime(2026, 10, 20),
                )
            )
        db.commit()

    def database():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = database
    try:
        client = TestClient(app)  # No lifespan: this test doesn't initialize real storage.
        first = client.get("/api/jobs?limit=20&offset=0")
        second = client.get("/api/jobs?limit=20&offset=20")
        assert first.status_code == second.status_code == 200
        a, b = first.json(), second.json()
        assert len(a) == 20 and len(b) == 5
        assert a[0]["id"] == f"{24:036d}"
        assert not {row["id"] for row in a} & {row["id"] for row in b}
        assert len(client.get("/api/jobs").json()) == 25
        for query in ["limit=0", "limit=101", "offset=-1"]:
            assert client.get("/api/jobs?" + query).status_code == 422
    finally:
        app.dependency_overrides.clear()
