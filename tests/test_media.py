"""Tests for glint.media using ffmpeg lavfi synthetic media."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from glint import config
from glint.media import InputRejected, grab_frame, normalize


def _gen_video(
    out: Path,
    duration_s: float,
    *,
    width: int = 640,
    height: int = 360,
    with_audio: bool = True,
) -> Path:
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={width}x{height}:rate=30:duration={duration_s}",
    ]
    if with_audio:
        cmd.extend(["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}"])
    cmd.extend(["-c:v", "libx264", "-preset", "ultrafast"])
    if with_audio:
        cmd.extend(["-c:a", "aac"])
    cmd.append(str(out))
    subprocess.run(cmd, check=True, capture_output=True)
    return out


async def test_normalize_valid_video(tmp_path: Path) -> None:
    src = _gen_video(tmp_path / "in.mp4", 4.0, with_audio=True)
    dst = tmp_path / "out.mp4"
    info = await normalize(src, dst)
    assert dst.is_file()
    assert info.height == config.NORMALIZED_HEIGHT
    assert info.width % 2 == 0
    assert info.has_audio is True
    assert info.trimmed is False
    assert info.duration_s >= 3.5


async def test_normalize_rejects_too_short(tmp_path: Path) -> None:
    src = _gen_video(tmp_path / "short.mp4", 2.0, with_audio=False)
    with pytest.raises(InputRejected) as exc_info:
        await normalize(src, tmp_path / "out.mp4")
    assert exc_info.value.code == "TOO_SHORT"


async def test_normalize_rejects_audio_only(tmp_path: Path) -> None:
    src = tmp_path / "audio_renamed.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(InputRejected) as exc_info:
        await normalize(src, tmp_path / "out.mp4")
    assert exc_info.value.code == "NO_VIDEO_STREAM"


async def test_normalize_rejects_corrupt_bytes(tmp_path: Path) -> None:
    src = tmp_path / "corrupt.mp4"
    src.write_bytes(b"not a valid video at all 1234567890")
    with pytest.raises(InputRejected) as exc_info:
        await normalize(src, tmp_path / "out.mp4")
    assert exc_info.value.code == "UNREADABLE"


async def test_normalize_trims_long_video(tmp_path: Path) -> None:
    src = _gen_video(tmp_path / "long.mp4", 100.0, width=320, height=180, with_audio=False)
    dst = tmp_path / "trimmed.mp4"
    info = await normalize(src, dst)
    assert info.trimmed is True
    assert info.has_audio is False
    assert 88.0 <= info.duration_s <= 91.5


async def test_grab_frame(tmp_path: Path) -> None:
    src = _gen_video(tmp_path / "clip.mp4", 4.0, with_audio=False)
    png = tmp_path / "frame.png"
    res = await grab_frame(src, 1.5, png)
    assert res == png
    assert png.is_file()
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # Clamping beyond duration still produces a frame
    png_end = tmp_path / "frame_end.png"
    await grab_frame(src, 999.0, png_end)
    assert png_end.is_file()
