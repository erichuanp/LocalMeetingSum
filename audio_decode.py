"""Decode any audio/video file to 16kHz mono float32 PCM via ffmpeg."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np


TARGET_SR = 16000


def _ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg not found on PATH")
    return p


def decode_to_pcm16k_mono(src: str | Path) -> np.ndarray:
    """Decode any media file (mp4, mov, mkv, m4a, mp3, wav, flac, opus, webm, 3gp, aac, ...)
       to float32 mono 16kHz PCM. Returns 1-D numpy array in [-1, 1]."""
    src = str(src)
    cmd = [
        _ffmpeg_path(), "-nostdin", "-loglevel", "error",
        "-i", src,
        "-vn",                  # drop video
        "-ac", "1",             # mono
        "-ar", str(TARGET_SR),  # 16k
        "-f", "f32le",          # raw float32 little-endian
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'ignore')[:500]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def probe_duration_seconds(src: str | Path) -> float:
    """ffprobe duration; returns 0.0 if unavailable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(proc.stdout.strip())
    except Exception:
        return 0.0
