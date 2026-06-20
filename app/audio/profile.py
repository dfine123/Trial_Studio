"""librosa beat map (gotcha #6).

    y, sr = librosa.load(path)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    beat_map = librosa.frames_to_time(beats, sr=sr).tolist()

`beat_drop_ts` and `structure` are set manually in the seed config (V1), not detected.
"""
from __future__ import annotations

from dataclasses import dataclass

import librosa
import numpy as np


@dataclass
class BeatProfile:
    bpm: float
    beat_map: list[float]
    duration: float


def analyze(path: str) -> BeatProfile:
    y, sr = librosa.load(path)  # mono, default sr=22050
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beats, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    duration = float(librosa.get_duration(y=y, sr=sr))
    return BeatProfile(
        bpm=round(bpm, 2),
        beat_map=[round(float(t), 4) for t in beat_times.tolist()],
        duration=round(duration, 3),
    )
