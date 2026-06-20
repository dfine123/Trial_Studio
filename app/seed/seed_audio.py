"""Seed the curated audio library with test audios.

V1 = manual curation: librosa generates the beat map (you confirm); description, the
before->after transition (`beat_drop_ts`), and `structure` are set by hand below.

Run:  python -m app.seed.seed_audio

Drop real audio files into samples/audio/ and edit SEED_AUDIOS to curate the real library.
(R2 upload is best-effort: rows + beat maps are written even if R2 isn't configured yet;
re-run after R2 credentials are fixed to upload the audio bytes.)
"""
from __future__ import annotations

import os

from sqlalchemy import select

from app.audio import profile
from app.db import SessionLocal
from app.models import Audio
from app.storage import r2

SEED_AUDIOS = [
    {
        "file": "samples/audio/a1_beforeafter_120.wav",
        "description": "Before/after flip — calm setup, hard switch at the drop into the flex payoff.",
        "beat_drop_ts": 6.0,
        "has_core_beat_drop": True,
        "structure": "before_after",
        "thematic_tags": ["flex", "transformation", "reveal"],
        "energy_arc": "low_then_high",
    },
    {
        "file": "samples/audio/a2_steady_100.wav",
        "description": "Steady builder — consistent energy, montage feel, no single pivot.",
        "beat_drop_ts": None,
        "has_core_beat_drop": False,
        "structure": "steady",
        "thematic_tags": ["lifestyle", "montage"],
        "energy_arc": "steady",
    },
]


def _r2_key(name: str) -> str:
    return f"audios/starter/{name}"


def seed() -> None:
    with SessionLocal() as s:
        for cfg in SEED_AUDIOS:
            path = cfg["file"]
            if not os.path.exists(path):
                print(f"SKIP (missing file): {path}")
                continue

            bp = profile.analyze(path)
            name = os.path.basename(path)
            key = _r2_key(name)

            uploaded = False
            try:
                with open(path, "rb") as fh:
                    r2.upload_fileobj(key, fh, content_type="audio/wav")
                uploaded = True
            except Exception as exc:  # noqa: BLE001 — best-effort; row still written
                print(f"  WARN: R2 upload failed for {name}: {exc}")

            audio = s.scalar(select(Audio).where(Audio.r2_key == key)) or Audio(
                r2_key=key, source="upload"
            )
            audio.description = cfg["description"]
            audio.bpm = bp.bpm
            audio.duration = bp.duration
            audio.beat_map = bp.beat_map
            audio.has_core_beat_drop = cfg["has_core_beat_drop"]
            audio.beat_drop_ts = cfg["beat_drop_ts"]
            audio.structure = cfg["structure"]
            audio.thematic_tags = cfg["thematic_tags"]
            audio.energy_arc = cfg["energy_arc"]
            if audio.id is None:
                s.add(audio)
            s.commit()

            print(
                f"SEEDED {name}: bpm={bp.bpm} beats={len(bp.beat_map)} "
                f"drop={cfg['beat_drop_ts']} structure={cfg['structure']} "
                f"r2_uploaded={uploaded}"
            )


if __name__ == "__main__":
    seed()
