"""Seed the curated audio library (the tracks the generator picks from).

V1 = manual curation: librosa generates the beat map; description/structure/tags are set by
hand below. The matching example captions are baked into the Caption Assistant's few-shot.

Run:  python -m app.seed.seed_audio
"""
from __future__ import annotations

import os

from sqlalchemy import delete, select

from app.audio import profile
from app.db import SessionLocal
from app.models import Audio
from app.storage import r2

# The 5 curated audios the creator supplied, each with its IG audio link (used in V2).
SEED_AUDIOS = [
    {
        "file": "samples/audio/ethereal_slowed_trap.mp3",
        "description": "Ethereal slowed trap — atmospheric, reflective, late-night flex.",
        "structure": "steady", "energy_arc": "low",
        "thematic_tags": ["slowed", "ethereal", "reflective", "late-night"],
        "ig_audio_url": "https://www.instagram.com/reels/audio/1262070545810368/",
    },
    {
        "file": "samples/audio/rap_intro_caption.mp3",
        "description": "Rap intro — punchy, confident spoken-word opener.",
        "structure": "steady", "energy_arc": "high",
        "thematic_tags": ["rap", "intro", "punchy", "contrarian"],
        "ig_audio_url": "https://www.instagram.com/reels/audio/27775783222008048/",
    },
    {
        "file": "samples/audio/bass_boosted_detroit.mp3",
        "description": "Bass-boosted Detroit rap — gritty, aggressive, hard.",
        "structure": "steady", "energy_arc": "high",
        "thematic_tags": ["detroit", "aggressive", "gritty", "numbers-flex"],
        "ig_audio_url": "https://www.instagram.com/reels/audio/36176137685366172/",
    },
    {
        "file": "samples/audio/positive_chainsmokers_lifestyle.mp3",
        "description": "Positive lifestyle — upbeat, bright, aspirational.",
        "structure": "steady", "energy_arc": "rising",
        "thematic_tags": ["upbeat", "lifestyle", "positive", "aspirational"],
        "ig_audio_url": "https://www.instagram.com/reels/audio/1332621673543509/",
    },
    {
        "file": "samples/audio/trap_upbeat_instrumental.mp3",
        "description": "Trap upbeat instrumental — punchy, hype, celebratory.",
        "structure": "steady", "energy_arc": "high",
        "thematic_tags": ["trap", "upbeat", "hype"],
        "ig_audio_url": "https://www.instagram.com/reels/audio/25730052399936515/",
    },
    # ── client-1 audios (titles = descriptions; thematic_tags = the user's category hint,
    #    used as a SOFT vibe steer for caption generation, never copied verbatim) ──
    {
        "file": "samples/audio/slowed_upbeat_house_dance.mp3",
        "description": "Slowed upbeat house dance track",
        "structure": "steady", "energy_arc": "rising",
        "thematic_tags": ["ironic-motivational", "glow-up", "how-life-feels-when", "house"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/detroit_bass_boosted_2.mp3",
        "description": "Detroit Bass boosted 2",
        "structure": "steady", "energy_arc": "high",
        "thematic_tags": ["tuff", "defiant", "hard", "chip-on-shoulder"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/minimal_slow_guitar.mp3",
        "description": "Minimalslow guitar instrumental",
        "structure": "steady", "energy_arc": "low",
        "thematic_tags": ["reflective", "wisdom", "hard-truth", "introspective"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/slowed_aspirational_housetrap.mp3",
        "description": "Slowed aspirational housetrap ethereal beat",
        "structure": "steady", "energy_arc": "low",
        "thematic_tags": ["business-realtalk", "building", "hindsight", "growth"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/slowed_house_upbeat_aspirational.mp3",
        "description": "Slowed house song upbeat but aspirational at the same time",
        "structure": "steady", "energy_arc": "rising",
        "thematic_tags": ["aspirational", "summer", "boys-and-money", "flex"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/upbeat_house_instrumental.mp3",
        "description": "Upbeat house beat instrumental",
        "structure": "steady", "energy_arc": "high",
        "thematic_tags": ["backhanded-motivation", "absurd-grind", "ironic-hype"],
        "ig_audio_url": None,
    },
    # ── batch 3 (2026-07-04). The quoted line in each description is the operator's VIBE HINT —
    #    it grounds the attitude the track suits (for caption steering + audio matching); captions
    #    aligned with that vibe fit, they don't have to mirror the example. ──
    {
        "file": "samples/audio/drake_more_slowed_rap.mp3",
        "description": "Drake-type extra-slowed rap — deadpan, self-aware degenerate-confession energy "
                       "(vibe: \"maybe it's the gambling that's addicted to me, ever thought about that?\")",
        "structure": "steady", "energy_arc": "low",
        "thematic_tags": ["slowed", "deadpan-confession", "self-aware-degen", "late-night"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/super_slowed_intro.mp3",
        "description": "Super slowed intro — heavy, lock-in urgency, already-behind motivation "
                       "(vibe: \"lock in bro you're only 3 years late\")",
        "structure": "steady", "energy_arc": "low",
        "thematic_tags": ["super-slowed", "lock-in", "urgency", "behind-schedule"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/ambient_house_instrumental.mp3",
        "description": "Ambient house — calm, blunt-positive reassurance / POV energy "
                       "(vibe: \"the grass gon be greener on whatever side we on\")",
        "structure": "steady", "energy_arc": "low",
        "thematic_tags": ["ambient", "blunt-positive", "pov", "we-gon-be-fine"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/slowed_trap_rap.mp3",
        "description": "Slowed trap rap — flex energy, ironic OR genuine, high-stakes degen swagger "
                       "(vibe: \"how me and bro living after we risked our parents' houses on blackjack\")",
        "structure": "steady", "energy_arc": "low",
        "thematic_tags": ["slowed-trap", "ironic-flex", "flex", "high-stakes"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/upbeat_hiphop_instrumental.mp3",
        "description": "Upbeat hip-hop — playful bit energy, joking-with-the-boys reaction vibe "
                       "(vibe: \"giving bro advice but adding 'that's just me tho' in case it ruins his life\")",
        "structure": "steady", "energy_arc": "high",
        "thematic_tags": ["upbeat", "playful", "bit-energy", "bro-reaction"],
        "ig_audio_url": None,
    },
    {
        "file": "samples/audio/ethereal_poppy_house.mp3",
        "description": "Ethereal poppy house — aspirational destiny, main-character it's-game-over energy "
                       "(vibe: \"when a millionaire tells you you sound like a younger version of him\")",
        "structure": "steady", "energy_arc": "rising",
        "thematic_tags": ["ethereal", "aspirational", "destiny", "main-character"],
        "ig_audio_url": None,
    },
]


def _r2_key(name: str) -> str:
    return f"audios/starter/{name}"


def seed() -> None:
    seeded_keys = set()
    with SessionLocal() as s:
        for cfg in SEED_AUDIOS:
            path = cfg["file"]
            if not os.path.exists(path):
                print(f"SKIP (missing file): {path}")
                continue

            bp = profile.analyze(path)
            name = os.path.basename(path)
            key = _r2_key(name)
            seeded_keys.add(key)

            try:
                with open(path, "rb") as fh:
                    r2.upload_fileobj(key, fh, content_type="audio/mpeg")
            except Exception as exc:  # noqa: BLE001 — best-effort; row still written
                print(f"  WARN: R2 upload failed for {name}: {exc}")

            audio = s.scalar(select(Audio).where(Audio.r2_key == key)) or Audio(
                r2_key=key, source="upload"
            )
            audio.description = cfg["description"]
            audio.bpm = bp.bpm
            audio.duration = bp.duration
            audio.beat_map = bp.beat_map
            audio.has_core_beat_drop = False
            audio.beat_drop_ts = None
            audio.structure = cfg["structure"]
            audio.thematic_tags = cfg["thematic_tags"]
            audio.energy_arc = cfg["energy_arc"]
            audio.ig_audio_url = cfg["ig_audio_url"]
            if audio.id is None:
                s.add(audio)
            s.commit()
            print(f"SEEDED {name}: bpm={bp.bpm} dur={bp.duration}s beats={len(bp.beat_map)} structure={cfg['structure']}")

        # drop any stale SEED audios not in the current curated set. CRITICAL: only ever touch seed
        # rows (r2_key under the starter prefix) — NEVER user uploads (whose r2_key is a var/ path),
        # or every redeploy wipes uploaded audios and orphans the templates that point at them.
        seed_prefix = "audios/starter/"
        stale = s.scalars(
            select(Audio).where(Audio.r2_key.like(seed_prefix + "%"), Audio.r2_key.notin_(seeded_keys))
        ).all()
        if stale:
            s.execute(delete(Audio).where(Audio.r2_key.like(seed_prefix + "%"), Audio.r2_key.notin_(seeded_keys)))
            s.commit()
            print(f"removed {len(stale)} stale seed audio rows")


if __name__ == "__main__":
    seed()
