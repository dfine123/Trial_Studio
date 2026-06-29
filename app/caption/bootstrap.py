"""Cold-start a new profile's VOICE corpus by reskinning a source profile's PROVEN FORMATS into the
new creator's voice.

The format/twist transfers (it's the shared base); the voice is the target's (their persona). Used
once for a profile with no real captions yet — then it generates + the user grades it down to the
creator's actual voice (keep / kill / not-voice-aligned). Gambling-flavored source refs are dropped,
since a reskin can't make those land for a non-gambling creator.
"""
from __future__ import annotations

import json
import os

from app import profiles
from app.caption.llm import complete_json
from app.corpus.store import load_refs

_RESKIN_SYS = """You reskin short-form captions from one creator's voice into ANOTHER creator's voice. You're given the TARGET creator's persona, then a numbered list of SOURCE captions.

For EACH source caption: KEEP its exact format — the same structure, the same twist mechanism, the same rhythm and sharpness — but rewrite it fully in the TARGET's voice and world. Change the subject/framing so it's unmistakably the TARGET (their topics, their angle), and DROP anything that doesn't fit them (e.g. being broke, gambling, self-pity). It must NOT read as a copy of the source's subject — same FORMAT, new line in the target's voice. Keep it hyper-specific and very-online. ONE caption per source, same count, same order.

Return ONLY JSON: {"captions": ["<reskinned 1>", "<reskinned 2>", ...]}"""


def reskin(source_captions: list[str], target_persona: str) -> list[str]:
    if not source_captions:
        return []
    user = ("TARGET CREATOR — this is the voice to write in:\n" + target_persona
            + "\n\nSOURCE CAPTIONS — reskin each into the target's voice (same format, same order):\n"
            + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(source_captions)))
    out = complete_json(_RESKIN_SYS, user, effort="high", max_tokens=4000)
    s, e = out.find("{"), out.rfind("}")
    if s == -1:
        return []
    try:
        return [c.strip() for c in json.loads(out[s:e + 1]).get("captions", []) if (c or "").strip()]
    except json.JSONDecodeError:
        return []


def bootstrap_from(target, source, limit: int = 40) -> int:
    """Reskin up to `limit` of the source profile's (non-gambling) refs into the target's voice and
    APPEND them to the target's corpus. Returns how many were added."""
    from app.caption.engine import _is_gambling   # lazy: avoid pulling the engine at module import

    src = [r for r in load_refs(profiles.corpus_path(source))
           if (r.get("caption") or "").strip() and not _is_gambling(r)][:limit]
    tp = (profiles.read_persona(target) or "").strip() or "an actually-cool, confident, very-online creator"

    new_refs: list[dict] = []
    for i in range(0, len(src), 12):                 # chunk so each LLM call stays small + reliable
        chunk = src[i:i + 12]
        caps = reskin([r["caption"] for r in chunk], tp)
        for r, c in zip(chunk, caps):
            new_refs.append({"caption": c, "persona_trait": r.get("persona_trait", "core")})

    path = profiles.corpus_path(target)
    start = len(load_refs(path)) + 1                 # continue ref-ids (append, never clobber real refs)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for j, nr in enumerate(new_refs):
            f.write(json.dumps({"ref_id": f"r{start + j:03d}", "caption": nr["caption"],
                                "source": "bootstrap", "persona_trait": nr["persona_trait"]},
                               ensure_ascii=False) + "\n")
    return len(new_refs)
