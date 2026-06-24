"""Random-word source for the seeding experiment — a REAL random word generator, no AI.

The word is DIVERGENCE FUEL: a loose perturbation fed per-caption to knock generation out of its
default lanes. It need not appear in, relate to, or apply to the output. Uses the offline
`wonderwords` generator (a genuine RWG, no network, no model); small static real-word fallback only
if the package is somehow missing.
"""
from __future__ import annotations

import random

try:
    from wonderwords import RandomWord as _RandomWord

    _RW = _RandomWord()

    def _one() -> str:
        # lean noun (most evocative as a spark), with occasional adjective/verb for variety
        pos = random.choice([["nouns"], ["nouns"], ["nouns"], ["adjectives"], ["verbs"]])
        try:
            return _RW.word(include_parts_of_speech=pos)
        except Exception:  # noqa: BLE001
            return _RW.word()

except Exception:  # noqa: BLE001 — package missing: fall back to a static list of real words
    _FALLBACK = (
        "lighthouse tractor cinnamon avalanche stapler walrus velvet compass orchard piston "
        "lantern marble cactus hammock gravel trombone pelican mosaic anchor pretzel glacier "
        "turbine maple thunder satchel ferret quartz meadow saddle bonfire umbrella domino kelp "
        "comet drawer cobweb molar pendulum raccoon syrup harbor blanket whistle cathedral"
    ).split()

    def _one() -> str:
        return random.choice(_FALLBACK)


def random_words(k: int) -> list[str]:
    """k random divergence-seed words."""
    return [_one() for _ in range(max(0, k))]
