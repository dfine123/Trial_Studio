"""OpenCV per-segment visual metrics + usability scoring.

For each segment we sample frames and compute luminance, color temperature, a dominant
palette, motion intensity, a subject bounding box, and a usability score.

Gotcha #5: usability_score (0-1) blends sharpness (variance of Laplacian), exposure
(penalize blown/crushed luminance), and stability (low inter-frame motion = steadier).
It's what stops blurry/shaky moments from being chosen. `energy` (0-1) is derived from
motion + luminance.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

# Tunables (v1 heuristics)
_SHARP_REF = 800.0          # variance-of-Laplacian that maps to ~1.0 sharpness
_CLIP_LO, _CLIP_HI = 16, 239  # 8-bit luminance considered crushed/blown beyond these

_FACE_CASCADE = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)


@dataclass
class SegmentMetrics:
    avg_luminance: float          # 0..1
    color_temp_k: float
    dominant_palette: list[str]   # hex strings
    motion_intensity: float       # 0..1
    sharpness: float              # 0..1
    usability_score: float        # 0..1
    energy: float                 # 0..1
    subject_bbox: dict | None     # normalized {x,y,w,h} or None
    shot_scale: str | None
    lighting: str | None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _sample_frames(cap, start_ts: float, end_ts: float, n: int = 8) -> list[np.ndarray]:
    end_ts = max(end_ts, start_ts + 1e-3)
    ts = np.linspace(start_ts, max(start_ts, end_ts - 1e-2), num=max(2, n))
    frames = []
    for t in ts:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
        ok, frame = cap.read()
        if ok and frame is not None:
            frames.append(frame)
    return frames


def _color_temp_k(bgr_mean: np.ndarray) -> float:
    """Approx correlated color temperature via McCamy's formula from mean BGR."""
    b, g, r = (float(c) / 255.0 for c in bgr_mean)
    # sRGB -> XYZ (no gamma linearization; rough but stable for v1)
    x_ = 0.4124 * r + 0.3576 * g + 0.1805 * b
    y_ = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z_ = 0.0193 * r + 0.1192 * g + 0.9505 * b
    s = x_ + y_ + z_
    if s <= 1e-6:
        return 6500.0
    x = x_ / s
    y = y_ / s
    denom = (0.1858 - y)
    if abs(denom) < 1e-6:
        return 6500.0
    n = (x - 0.3320) / denom
    cct = 437 * n**3 + 3601 * n**2 + 6861 * n + 5517
    return float(max(1000.0, min(40000.0, cct)))


def _dominant_palette(frame: np.ndarray, k: int = 4, sample: int = 2000) -> list[str]:
    pix = frame.reshape(-1, 3).astype(np.float32)
    if len(pix) > sample:
        idx = np.random.default_rng(0).choice(len(pix), sample, replace=False)
        pix = pix[idx]
    k = min(k, max(1, len(np.unique(pix, axis=0))))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    _, labels, centers = cv2.kmeans(pix, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.flatten(), minlength=k)
    order = np.argsort(counts)[::-1]
    palette = []
    for i in order:
        b, g, r = centers[i].astype(int)
        palette.append(f"#{r:02x}{g:02x}{b:02x}")
    return palette


def _subject_bbox(frame: np.ndarray) -> dict | None:
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = _FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
    if len(faces) == 0:
        return None
    fx, fy, fw, fh = max(faces, key=lambda b: b[2] * b[3])
    return {
        "x": round(fx / w, 4), "y": round(fy / h, 4),
        "w": round(fw / w, 4), "h": round(fh / h, 4),
    }


def _shot_scale(bbox: dict | None) -> str | None:
    if not bbox:
        return None
    area = bbox["w"] * bbox["h"]
    if area > 0.18:
        return "close_up"
    if area > 0.05:
        return "medium"
    return "wide"


def _lighting(lum: float) -> str:
    if lum < 0.25:
        return "low_key"
    if lum > 0.7:
        return "high_key"
    return "balanced"


def analyze_segment(path: str, start_ts: float, end_ts: float, n_frames: int = 8) -> SegmentMetrics:
    cap = cv2.VideoCapture(path)
    try:
        frames = _sample_frames(cap, start_ts, end_ts, n_frames)
    finally:
        cap.release()

    if not frames:
        return SegmentMetrics(0.0, 6500.0, [], 0.0, 0.0, 0.0, 0.0, None, None, "balanced")

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]

    # luminance (0..1)
    lum = float(np.mean([g.mean() for g in grays]) / 255.0)

    # sharpness via variance of Laplacian
    sharp_raw = float(np.mean([cv2.Laplacian(g, cv2.CV_64F).var() for g in grays]))
    sharpness = _clamp01(sharp_raw / _SHARP_REF)

    # exposure: penalize crushed/blown pixels
    clipped = float(np.mean([
        np.mean((g <= _CLIP_LO) | (g >= _CLIP_HI)) for g in grays
    ]))
    exposure = _clamp01(1.0 - clipped)

    # motion: mean abs diff between successive sampled frames (0..1)
    if len(grays) > 1:
        diffs = [np.mean(np.abs(grays[i].astype(np.int16) - grays[i - 1].astype(np.int16))) / 255.0
                 for i in range(1, len(grays))]
        motion = _clamp01(float(np.mean(diffs)) * 4.0)  # scale: small mean diffs -> meaningful motion
    else:
        motion = 0.0

    stability = _clamp01(1.0 - motion)
    usability = _clamp01(0.5 * sharpness + 0.3 * exposure + 0.2 * stability)
    energy = _clamp01(0.6 * motion + 0.4 * lum)

    mid = frames[len(frames) // 2]
    bgr_mean = np.array(mid).reshape(-1, 3).mean(axis=0)
    color_temp = _color_temp_k(bgr_mean)
    palette = _dominant_palette(mid)
    bbox = _subject_bbox(mid)

    return SegmentMetrics(
        avg_luminance=round(lum, 4),
        color_temp_k=round(color_temp, 1),
        dominant_palette=palette,
        motion_intensity=round(motion, 4),
        sharpness=round(sharpness, 4),
        usability_score=round(usability, 4),
        energy=round(energy, 4),
        subject_bbox=bbox,
        shot_scale=_shot_scale(bbox),
        lighting=_lighting(lum),
    )
