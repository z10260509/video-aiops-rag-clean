import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


CODEC_OPTIONS = {
    "h264": ("libx264", {"high": "20", "balanced": "23", "small": "28"}),
    "h265": ("libx265", {"high": "24", "balanced": "28", "small": "32"}),
}


def transcode_command(
    input_path: Path,
    output_path: Path,
    resolution: str,
    video_codec: str = "h264",
    compression_preset: str = "balanced",
) -> list[str]:
    height = {"720p": "720", "480p": "480"}.get(resolution)
    if height is None:
        raise ValueError(f"Unsupported target resolution: {resolution}")
    try:
        encoder, crf_by_preset = CODEC_OPTIONS[video_codec]
        crf = crf_by_preset[compression_preset]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported codec or compression preset: {video_codec}/{compression_preset}"
        ) from exc
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        f"scale=-2:{height}",
        "-c:v",
        encoder,
        "-crf",
        crf,
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def transcode_video(
    input_path: Path,
    output_path: Path,
    resolution: str,
    video_codec: str = "h264",
    compression_preset: str = "balanced",
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        transcode_command(input_path, output_path, resolution, video_codec, compression_preset),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise FFmpegError(result.stderr.strip() or "FFmpeg failed without an error message")


def create_thumbnail(video_path: Path, thumbnail_path: Path) -> None:
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(thumbnail_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        thumbnail_path.unlink(missing_ok=True)
        raise FFmpegError(result.stderr.strip() or "FFmpeg thumbnail generation failed")
