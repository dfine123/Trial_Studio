"""Segmentation — PySceneDetect shot boundaries + long-take windowing.

Gotcha #3: PySceneDetect only cuts on hard scene changes, so a long continuous take
returns ONE scene and naive selection would always grab its head. After detection, we
window any segment longer than ~5s into overlapping ~3s candidate sub-segments, so every
take yields multiple usable moments. This is the whole point of segment-level indexing.
"""
from __future__ import annotations

from dataclasses import dataclass

from scenedetect import ContentDetector, detect


@dataclass
class Window:
    start_ts: float
    end_ts: float
    duration: float
    source: str  # "scene" | "window"


def detect_scenes(path: str, threshold: float = 27.0) -> list[tuple[float, float]]:
    """Return [(start_s, end_s)]. Empty if PySceneDetect finds no cuts (single take)."""
    scenes = detect(path, ContentDetector(threshold=threshold))
    return [(s.get_seconds(), e.get_seconds()) for s, e in scenes]


def window_long_take(
    start: float,
    end: float,
    target: float = 3.0,
    overlap: float = 1.0,
    min_window: float = 2.0,
) -> list[tuple[float, float]]:
    """Split [start, end] into overlapping ~target-second windows."""
    step = max(target - overlap, 0.5)
    windows: list[tuple[float, float]] = []
    t = start
    while t < end - 1e-6:
        w_end = min(t + target, end)
        if (w_end - t) >= min_window or not windows:
            windows.append((t, w_end))
        if w_end >= end:
            break
        t += step
    return windows


def segment_video(
    path: str,
    total_duration: float,
    long_take_threshold: float = 5.0,
    target: float = 3.0,
    overlap: float = 1.0,
    threshold: float = 27.0,
) -> list[Window]:
    scenes = detect_scenes(path, threshold=threshold)
    if not scenes:
        scenes = [(0.0, total_duration)]  # no cuts → one continuous take

    out: list[Window] = []
    for s, e in scenes:
        dur = e - s
        if dur > long_take_threshold:
            for ws, we in window_long_take(s, e, target, overlap):
                out.append(Window(ws, we, we - ws, "window"))
        else:
            out.append(Window(s, e, dur, "scene"))
    return out
