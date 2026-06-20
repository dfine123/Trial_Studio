"""QC gate — ffprobe a video, parse dims/fps/duration/bitrate, gate on resolution + fps.

Gotcha #1: ffprobe `r_frame_rate` is a fraction like "60000/1001" — parse it; fall back to
`avg_frame_rate` if it's "0/0". Gate on the SMALLER dimension so vertical and horizontal
source are both judged correctly.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass
class ProbeResult:
    width: int
    height: int
    fps: float
    duration: float
    bitrate: int | None


@dataclass
class QCResult:
    passed: bool
    reason: str | None
    probe: ProbeResult


def _parse_fps(stream: dict) -> float:
    for key in ("r_frame_rate", "avg_frame_rate"):
        val = stream.get(key)
        if val and val != "0/0":
            try:
                num, den = (int(x) for x in val.split("/"))
            except (ValueError, TypeError):
                continue
            if den:
                return num / den
    return 0.0


def ffprobe(path: str) -> ProbeResult:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,bit_rate,duration:format=duration,bit_rate",
        "-of", "json",
        path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    fps = _parse_fps(stream)
    duration = float(fmt.get("duration") or stream.get("duration") or 0.0)

    bitrate = None
    for src in (stream.get("bit_rate"), fmt.get("bit_rate")):
        if src and str(src).isdigit():
            bitrate = int(src)
            break

    return ProbeResult(width=width, height=height, fps=fps, duration=duration, bitrate=bitrate)


def check(path: str, min_resolution: int = 1080, min_fps: float = 29.9) -> QCResult:
    """Reject if min(width, height) < min_resolution OR fps < min_fps."""
    p = ffprobe(path)
    short_side = min(p.width, p.height) if p.width and p.height else 0
    if short_side < min_resolution:
        return QCResult(
            False,
            f"resolution too low: {p.width}x{p.height} "
            f"(short side {short_side}px < {min_resolution}px required)",
            p,
        )
    if p.fps < min_fps:
        return QCResult(
            False,
            f"frame rate too low: {p.fps:.2f}fps (< {min_fps}fps required)",
            p,
        )
    return QCResult(True, None, p)
