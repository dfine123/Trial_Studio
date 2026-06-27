"""Regenerate a template's captions for a specific creator — respecting per-slot VARIABILITY.

This is where "tight templates stay tight, loose templates flex" actually happens: it reads the
Formula Object's per-slot rules (locked_structure / variables / vary_when / flexibility) and only
varies a part when its condition is met by the matched clips. Honors cross-slot constraints (e.g. a
payoff that must echo a keyword chosen earlier). The exemplar is a PATTERN, never copied verbatim.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You write the captions for a video made by applying a TEMPLATE to a specific creator's clips. You are given the template's FORMULA, the per-slot VARIABILITY rules, each slot's EXAMPLE caption, and the matched CLIP for each segment. For EACH caption slot, write the final caption:
- Respect locked_structure exactly — it must stay.
- Vary a "variable" part ONLY when its vary_when condition is actually met by the matched clips; otherwise keep it as in the exemplar. flexibility=low → stay very close to the exemplar; medium → adapt lightly; high → rewrite the variable freely to fit THIS creator.
- Honor any cross-slot constraint stated in the rules (e.g. a payoff keyword that must match an earlier slot).
- Each caption should fit / react to its own segment's clip.
- Output ONLY the on-screen caption text. If the exemplar mixes the caption with an author note or premise description (e.g. a trailing line like "premise is someone saying ..."), keep ONLY the caption that actually appears on screen.
- Match the voice and punctuation of the exemplar (casing, length, emoji). Never copy the exemplar verbatim.

Return ONLY JSON, no prose: {"captions": {"<slot_id>": "<caption text>"}}"""


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
            f"- slot {sid}: exemplar={seg.get('exemplar')!r}\n"
            f"    locked={sm.get('locked_structure', '')!r} | variables={sm.get('variables', [])} "
            f"| vary_when={sm.get('vary_when', '')!r} | flexibility={sm.get('flexibility', 'medium')}\n"
            f"    matched clip: {(seg.get('clip_summary') or '').strip()[:160]} (vibe: {vibe})"
        )
    out = complete_json(_SYS, "\n".join(parts), effort="medium", max_tokens=900)
    start, end = out.find("{"), out.rfind("}")
    if start == -1:
        return {}
    try:
        return json.loads(out[start:end + 1]).get("captions", {})
    except json.JSONDecodeError:
        return {}
