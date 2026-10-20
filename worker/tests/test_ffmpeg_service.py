from pathlib import Path

import pytest

from worker.app.ffmpeg_service import (
    FFmpegError,
    create_thumbnail,
    transcode_command,
    transcode_video,
)


def test_transcode_command_uses_expected_scale_and_output():
    command = transcode_command(Path("input.mp4"), Path("output.mp4"), "720p")
    assert command[:4] == ["ffmpeg", "-y", "-i", "input.mp4"]
    assert "scale=-2:720" in command
    assert command[-1] == "output.mp4"


def test_transcode_command_uses_selected_codec_and_quality():
    command = transcode_command(Path("input.mp4"), Path("output.mp4"), "480p", "h265", "small")
    assert "libx265" in command
    assert command[command.index("-crf") + 1] == "32"


def test_transcode_failure_removes_partial_output(tmp_path, monkeypatch):
    output = tmp_path / "output.mp4"
    output.write_bytes(b"partial")

    class Result:
        returncode = 1
        stderr = "bad input"

    monkeypatch.setattr(
        "worker.app.ffmpeg_service.subprocess.run", lambda *args, **kwargs: Result()
    )
    with pytest.raises(FFmpegError, match="bad input"):
        transcode_video(tmp_path / "input.mp4", output, "480p")
    assert not output.exists()


def test_thumbnail_failure_removes_partial_output(tmp_path, monkeypatch):
    thumbnail = tmp_path / "thumbnail.jpg"
    thumbnail.write_bytes(b"partial")

    class Result:
        returncode = 1
        stderr = "could not create thumbnail"

    monkeypatch.setattr(
        "worker.app.ffmpeg_service.subprocess.run", lambda *args, **kwargs: Result()
    )
    with pytest.raises(FFmpegError, match="could not create thumbnail"):
        create_thumbnail(tmp_path / "input.mp4", thumbnail)
    assert not thumbnail.exists()
