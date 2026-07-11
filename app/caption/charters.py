"""THE FIVE ENGINES — v3 generation charters, KERNEL form.

2026-07-10 full re-alignment (operator: "align way more with the original references and the
highly graded ones — the current ones are still too bad all around"): every prior charter
iteration grew the instruction mass until it competed with the references for attention. The
references are the teacher now — each engine's context is dominated by the operator's material
(the full feed + THE ONES THAT HIT HARDEST at max salience) and the charter shrinks to a KERNEL:
its one interaction, in a few plain sentences. Craft laws with measured wins live in the shared
tail; everything else was description, and description is lossy.

Each engine still believes it IS the caption writer. No kernel mentions engines, slates, slots,
or options; none quotes winners. Operator-editable: var/charters/<id>.md overrides.
"""
from __future__ import annotations

import os

_DIR = os.path.join("var", "charters")


SCREENSHOT = """
Tonight's post is the one a guy saves for himself at 1am — it stings and pushes in the same words. It's about a type of man, or about you said with full chest — never aimed at the reader; he does the aiming at himself in private. Blunt beats clever, the exact detail does the work, and it's true any week of any year.
"""

SEND = """
Tonight's post is the one a guy forwards — he laughs, thinks of exactly one person, and sends it. So it's about someone: a type of guy, a thing dudes say, a girl-and-money situation, the friend everybody has. Something in it clicks — a word true two ways, math that checks out, a flip — and the reader catches it himself. Don't be afraid to say the thing you're not supposed to say, calmly.
"""

EXOTIC = """
Tonight's post is a play nobody's seen before — no formats, no templates, no shape you could name from your feed or anyone's. But it's still you: same world, same mouth, one plain spoken thought with a real point under the strangeness, airtight when read literally. New construction, plain delivery.
"""

MIRROR = """
Tonight's post catches people doing the thing they thought nobody saw — mfs, dudes, broke mfs, broke 🥷s, the guy who (never "a broke dude"). A real behavior you actually clocked, told flat with the exact damning detail, no lesson attached. The reader casts himself — that's the whole catch.
"""

MENACE = """
Tonight's post is you mid-scene — a cop, a girl, a client, a teller hands you a line, and your reply is too honest and too confident at the same time. Their line, a beat, your line. The delusion always wins: never sad, never sorry, never in on the joke. The logic almost holds — that's what they love you for.
"""


ENGINES = [
    {"id": "screenshot", "name": "the screenshot (motivate)", "charter": SCREENSHOT},
    {"id": "send", "name": "the send (shareable)", "charter": SEND},
    {"id": "exotic", "name": "the exotic (pure principle)", "charter": EXOTIC},
    {"id": "mirror", "name": "the mirror (recognition)", "charter": MIRROR},
    {"id": "menace", "name": "the menace (character)", "charter": MENACE},
]


def charter(engine_id: str) -> str:
    """The engine's charter: operator-edited file if present, else the seed constant."""
    try:
        with open(os.path.join(_DIR, f"{engine_id}.md"), encoding="utf-8") as f:
            t = f.read().strip()
        if len(t) >= 100:
            return "\n" + t + "\n"
    except Exception:  # noqa: BLE001
        pass
    for e in ENGINES:
        if e["id"] == engine_id:
            return e["charter"]
    raise KeyError(f"unknown engine: {engine_id}")


def save_charter(engine_id: str, text: str) -> int:
    if engine_id not in {e["id"] for e in ENGINES}:
        raise KeyError(f"unknown engine: {engine_id}")
    t = (text or "").strip()
    if len(t) < 100:
        raise ValueError("charter suspiciously short — refusing")
    os.makedirs(_DIR, exist_ok=True)
    path = os.path.join(_DIR, f"{engine_id}.md")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(t)
    os.replace(tmp, path)
    return len(t)
