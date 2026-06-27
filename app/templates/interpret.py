"""LLM interpretation of a Template -> a variability-aware Formula Object.

This is where the intelligence the user asked for lives: read the author's free-text segments /
clip-types / caption exemplars / role notes and reason out (a) what the template does, and
(b) — crucially — HOW MUCH each part can vary when re-skinned onto a different creator, and under
what conditions. Tight templates ("Poor?") stay tight and only flex a keyword if the clips earn it;
loose templates ("Watch me/bet") have wide fill-in-the-blanks. The LEVEL is inferred per template
from the author's own hints, never imposed.
"""
from __future__ import annotations

import json

from app.caption.llm import complete_json

_SYS = """You are the interpreter for a short-form video TEMPLATE STUDIO. An author built a template by marking beat-synced segments on an audio and, per segment, writing — in their own words — WHAT KIND OF CLIP goes there and an EXAMPLE caption (with an optional role note). Read THIS specific template and articulate how it works AND, most importantly, how much each part can VARY when it is re-skinned onto a DIFFERENT creator's clips.

Templates differ wildly in variability. Some are TIGHT: the structure and most wording are fixed, and only a small piece can change — and only when the creator's clips clearly support it ("if the stars align"). Others are LOOSE: a slot is essentially fill-in-the-blank, rewritten per creator. The author has ENCODED this in what they wrote — clip descriptions like "can audible to X if not there", caption variables like "(insert)", alternatives like "X or Y", and role notes like "structure stays but the keyword can change if the clips set up a better one". INFER the level from their hints; do NOT impose one. The exemplar caption is a PATTERN to honor, never copied verbatim.

Return ONLY JSON, no prose:
{
  "title": "<short name for the formula>",
  "formula": "<what this template does and why it lands>",
  "caption_logic": "<how the captions work and relate across the segments>",
  "reskin_rules": "<how to apply this to a NEW creator's clips while honoring the variability AND the authored clip flexibility (the 'can audible to ...' fallbacks)>",
  "slots": [
    {
      "slot_id": "<the caption slot id, e.g. s0>",
      "locked_structure": "<what MUST stay the same in this caption>",
      "variables": ["<each part that may change, e.g. \\"the keyword 'poor'\\", \\"the (insert) doubt\\">"],
      "vary_when": "<the condition under which to actually vary it, e.g. 'only if the matched clips strongly set up a stronger keyword; otherwise keep it as-is'>",
      "flexibility": "low | medium | high"
    }
  ]
}"""


def _digest(spec: dict) -> str:
    slots = {c.get("id"): c for c in spec.get("caption_slots", [])}
    lines = []
    for seg in sorted(spec.get("segments", []), key=lambda s: s.get("index", 0)):
        cc = seg.get("clip_criteria") or {}
        sid = seg.get("caption_slot_id")
        sl = slots.get(sid, {})
        cap = sl.get("exemplar")
        role = sl.get("role")
        lines.append(
            f"Segment {seg.get('index')} [{seg.get('t_in')}-{seg.get('t_out')}s]\n"
            f"  clip wanted: {cc.get('clip_type') or '(any)'}\n"
            f"  caption slot {sid or '(none)'}: exemplar={cap!r}" + (f"  role={role!r}" if role else "")
        )
    return "TEMPLATE:\n" + "\n".join(lines)


def interpret_template(spec: dict) -> dict:
    """Read a template spec, return the variability-aware Formula Object (dict)."""
    out = complete_json(_SYS, _digest(spec), effort="medium", max_tokens=1400)
    start, end = out.find("{"), out.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(out[start:end + 1])
    except json.JSONDecodeError:
        return {}
