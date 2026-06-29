"""Regenerate a template's captions for a specific creator — IN THE CREATOR'S VOICE, respecting
per-slot VARIABILITY.

Two things have to be true at once: the template's FORMAT must survive (a "watch me / bet" stays a
"watch me / bet"), and the words must sound unmistakably like THIS creator (Spence — terminally-online,
money-brained, the twist), not a neutral motivational voice. So we graft the comedy engine's actual
voice embodiment (persona + real reference captions) onto the template-filling task: the template is
the SKELETON, the creator's voice is the SKIN. Per-slot rules decide where the voice gets to flex —
locked_structure stays, high-flexibility variables get the full voice treatment.
"""
from __future__ import annotations

import json
import random

from app.caption.engine import voice_system
from app.caption.llm import complete_json
from app.corpus.store import load_refs

_TEMPLATE_RULES = """---

NOW: you are not free-writing a standalone joke. You're filling the captions for a TEMPLATE this creator is applying to their own clips. The template is the SKELETON — a proven format with a fixed shape — and YOUR VOICE is the skin. You're given the FORMULA, the per-slot VARIABILITY rules, each slot's EXAMPLE caption, and the matched CLIP for each segment.

For EACH caption slot, write the FINAL on-screen caption:
- Output ONLY the words that appear ON SCREEN. NEVER include author notes or premise descriptions (drop anything like "premise is someone saying...").
- locked_structure stays EXACTLY — it is the format's spine, do not rewrite or "improve" it.
- Vary a "variable" part ONLY when its vary_when condition is met by the clips. flexibility=low → stay very close to the exemplar (barely touch it); medium → adapt lightly; high → rewrite the variable freely IN YOUR VOICE.
- The VARIABLE is where your voice lives. Fill it so it's unmistakably YOU — hyper-specific, money-brained, very-online, with the twist — NOT a literal description of the clip. For a "you can't [do X]" doubt, X is a real come-up a broke-but-pre-rich guy actually gets doubted on, said your way. BAD: "you cant make money by doing pushups" / "running on the beach" — that just narrates the clip and has zero voice. GOOD: something sharp, specific, and postable that sounds like your real captions above.
- Honor cross-slot constraints (e.g. a payoff that must echo a keyword chosen in an earlier slot).
- Keep the casing, length, and energy of the exemplar. Never copy the exemplar verbatim.

Return ONLY JSON, no prose: {"captions": {"<slot_id>": "<caption text>"}}"""


def _voice_sys() -> str:
    """Spence's voice embodiment (persona + a sample of his real captions) + the template rules."""
    refs = [(r.get("caption") or "").strip() for r in load_refs() if (r.get("caption") or "").strip()]
    random.shuffle(refs)
    return voice_system("\n\n".join(refs[:24])) + "\n\n" + _TEMPLATE_RULES


def _clean_exemplar(ex: str | None) -> str:
    """The author may have typed the caption AND a note in one field (caption, blank line, note).
    The on-screen caption is the first block — drop the rest so the note never reaches the screen."""
    return ((ex or "").split("\n\n")[0]).strip()


def regenerate_captions(formula: dict, segments: list[dict]) -> dict:
    """formula: the Formula Object (incl. .slots). segments: [{index, slot_id, exemplar, clip_summary, clip_vibe}].
    Returns {slot_id: caption_text}, written in the creator's voice."""
    if not segments:
        return {}
    slots_meta = {s.get("slot_id"): s for s in formula.get("slots", [])}
    parts = [
        f"FORMULA: {formula.get('formula', '')}",
        f"CAPTION LOGIC: {formula.get('caption_logic', '')}",
        f"RESKIN RULES: {formula.get('reskin_rules', '')}",
        "",
        "SLOTS (in order):",
    ]
    for seg in segments:
        sid = seg.get("slot_id")
        sm = slots_meta.get(sid, {})
        vibe = ", ".join((seg.get("clip_vibe") or [])[:5])
        parts.append(
            f"- slot {sid}: exemplar={_clean_exemplar(seg.get('exemplar'))!r}\n"
            f"    locked={sm.get('locked_structure', '')!r} | variables={sm.get('variables', [])} "
            f"| vary_when={sm.get('vary_when', '')!r} | flexibility={sm.get('flexibility', 'medium')}\n"
            f"    matched clip: {(seg.get('clip_summary') or '').strip()[:160]} (vibe: {vibe})"
        )
    out = complete_json(_voice_sys(), "\n".join(parts), effort="high", max_tokens=900)
    start, end = out.find("{"), out.rfind("}")
    if start == -1:
        return {}
    try:
        caps = json.loads(out[start:end + 1]).get("captions", {})
    except json.JSONDecodeError:
        return {}
    # belt-and-suspenders: drop any meta-note that still slipped through
    return {k: _clean_exemplar(v) for k, v in caps.items()}
