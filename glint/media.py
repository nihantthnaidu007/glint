"""Video normalization and frame extraction via ffmpeg/ffprobe."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from glint import config
from glint.schemas import VideoInfo

logger = logging.getLogger(__name__)


class InputRejected(Exception):
    """Raised when an input video fails validation or normalization."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


async def _run_proc(cmd: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    """Run a subprocess without shell=True and enforce a timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout, stderr


async def _ffprobe(path: Path, timeout_s: float = 10.0) -> dict[str, Any]:
    """Probe a media file and return parsed ffprobe JSON."""
    if not path.is_file():
        raise InputRejected("UNREADABLE", f"Input file not found: {path.name}.")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        rc, stdout, stderr = await _run_proc(cmd, timeout_s=timeout_s)
    except TimeoutError as exc:
        raise InputRejected("UNREADABLE", "Timed out while inspecting video.") from exc
    if rc != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        logger.debug("ffprobe failed for %s: %s", path, err)
        raise InputRejected("UNREADABLE", "Could not read video file.")
    try:
        return json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise InputRejected("UNREADABLE", "Could not parse video metadata.") from exc


def _extract_probe_fields(probe: dict[str, Any]) -> tuple[float, int, int, bool]:
    """Extract (duration_s, width, height, has_audio) from ffprobe JSON."""
    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise InputRejected("NO_VIDEO_STREAM", "File does not contain a video stream.")
    v = video_streams[0]
    width = int(v.get("width") or 0)
    height = int(v.get("height") or 0)
    if width <= 0 or height <= 0:
        raise InputRejected("NO_VIDEO_STREAM", "Video stream has invalid dimensions.")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    raw_dur = v.get("duration") or probe.get("format", {}).get("duration")
    try:
        duration_s = float(raw_dur) if raw_dur is not None else 0.0
    except (TypeError, ValueError):
        duration_s = 0.0

    return duration_s, width, height, has_audio


async def normalize(src: Path, dst: Path) -> VideoInfo:
    """Normalize input video to mp4 h264 720p, aac if audio, keeping last MAX_VIDEO_S."""
    probe = await _ffprobe(src)
    duration_s, _, _, has_audio = _extract_probe_fields(probe)

    if duration_s < config.MIN_VIDEO_S:
        raise InputRejected(
            "TOO_SHORT",
            f"Video is {duration_s:.1f}s, minimum is {config.MIN_VIDEO_S:.1f}s.",
        )

    trimmed = duration_s > config.MAX_VIDEO_S
    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y"]
    if trimmed:
        start_s = max(0.0, duration_s - config.MAX_VIDEO_S)
        cmd.extend(["-ss", f"{start_s:.3f}"])
    cmd.extend(["-i", str(src)])
    if trimmed:
        cmd.extend(["-t", f"{config.MAX_VIDEO_S:.3f}"])

    cmd.extend(["-map", "0:v:0"])
    if has_audio:
        cmd.extend(["-map", "0:a:0"])

    cmd.extend(
        [
            "-vf",
            f"scale=-2:{config.NORMALIZED_HEIGHT}",
            "-fpsmax",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if has_audio:
        cmd.extend(["-c:a", "aac"])
    else:
        cmd.append("-an")
    cmd.extend(["-movflags", "+faststart", str(dst)])

    try:
        rc, _, stderr = await _run_proc(cmd, timeout_s=float(config.TIMEOUT_S["normalize"]))
    except TimeoutError as exc:
        raise InputRejected("UNREADABLE", "Video normalization timed out.") from exc

    if rc != 0 or not dst.is_file():
        err = stderr.decode("utf-8", errors="replace").strip()
        logger.debug("ffmpeg normalize failed: %s", err)
        raise InputRejected("UNREADABLE", "Failed to normalize video.")

    out_probe = await _ffprobe(dst)
    out_dur, width, height, out_audio = _extract_probe_fields(out_probe)
    return VideoInfo(
        path=str(dst),
        duration_s=out_dur,
        width=width,
        height=height,
        has_audio=out_audio,
        trimmed=trimmed,
    )


async def grab_frame(video: Path, t_s: float, out_png: Path) -> Path:
    """Extract a single frame at timestamp t_s (clamped to [0, duration]) as a PNG."""
    probe = await _ffprobe(video)
    duration_s, _, _, _ = _extract_probe_fields(probe)
    max_t = max(0.0, duration_s - 0.05) if duration_s > 0.05 else 0.0
    clamped_t = max(0.0, min(t_s, max_t))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{clamped_t:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        str(out_png),
    ]
    rc, _, stderr = await _run_proc(cmd, timeout_s=10.0)
    if rc != 0 or not out_png.is_file():
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg grab_frame failed: {err}")
    return out_png
