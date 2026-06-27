"""Regenerate a template's captions for a specific creator — respecting per-slot VARIABILITY.

This is where "tight templates stay tight, loose templates flex" actually happens: it reads the
Formula Object's per-slot rules (locked_structure / variables / vary_when / flexibility) and only
varies a part when its condition is met by the matched clips. Honors cross-slot constraints (e.g. a
payoff that must echo a keyword chosen earlier). The exemplar is a PATTERN, never copied verbatim.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You write the captions for a video made by applying a TEMPLATE to a specific creator's clips. You are given the template's FORMULA, the per-slot VARIABILITY rules, each slot's EXAMPLE caption, and the matched CLIP for each segment.

For EACH caption slot, write the FINAL on-screen caption:
- Output ONLY the words that appear ON SCREEN. NEVER include author notes, premise descriptions, or explanations of how it works (drop anything like "premise is someone saying...").
- Respect locked_structure exactly — it stays.
- Vary a "variable" part ONLY when its vary_when condition is met by the clips; otherwise keep it like the exemplar. flexibility=low → stay very close to the exemplar; medium → adapt lightly; high → rewrite the variable freely.
- When you fill a variable, write what the ARC IS SELLING in the template's VOICE — NOT a literal description of the clip. For a "you can't [do X]" style doubt, X must be a real ambition/come-up someone actually gets doubted on (making it, getting rich, blowing up, leaving the 9-5), and the payoff must visibly disprove it. BAD: "you cant make money by doing pushups" or "running on the beach" — that just narrates the setup clip. GOOD: "you cant make money posting videos" / "you'll never make it out of here". Write the sharpest, most postable version a real person would say.
- Honor cross-slot constraints (e.g. a payoff keyword that must match an earlier slot).
- Match the voice, casing, length, and emoji of the exemplar. Never copy the exemplar verbatim.

Return ONLY JSON, no prose: {"captions": {"<slot_id>": "<caption text>"}}"""


def _clean_exemplar(ex: str | None) -> str:
    """The author may have typed the caption AND a note in one field (caption, blank line, note).
    The on-screen caption is the first block — drop the rest so the note never reaches the screen."""
    return ((ex or "").split("\n\n")[0]).strip()


def regenerate_captions(formula: dict, segments: list[dict]) -> dict:
    """formula: the Formula Object (incl. .slots). segments: [{index, slot_id, exemplar, clip_summary, clip_vibe}].
    Returns {slot_id: caption_text}."""
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
    out = complete_json(_SYS, "\n".join(parts), effort="medium", max_tokens=900)
    start, end = out.find("{"), out.rfind("}")
    if start == -1:
        return {}
    try:
        caps = json.loads(out[start:end + 1]).get("captions", {})
    except json.JSONDecodeError:
        return {}
    # belt-and-suspenders: drop any meta-note that still slipped through
    return {k: _clean_exemplar(v) for k, v in caps.items()}
