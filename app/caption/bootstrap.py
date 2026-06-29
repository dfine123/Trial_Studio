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

_RESKIN_SYS = """You are LIGHTLY adapting captions from one creator's voice to a VERY SIMILAR creator's voice. The two are nearly the same — same degen, very-online, anti-simp humor — so MOST lines already fit the target and should come back exactly or almost exactly as they are.

For EACH source caption, do the MINIMUM:
- If it already fits the target, return it UNCHANGED. Most will: girls, your boys, dating, status, loyalty, relatable bro takes all fit him directly — leave them alone.
- ONLY edit the specific part that doesn't fit: drop self-pity / him calling HIMSELF broke (he is NOT broke), drop gambling, soften anything that clashes with an easy, unbothered, confident vibe. Change just that part; keep the rest verbatim.
- Do NOT swap a subject that works (NEVER flip "your girl" to "your boy"). Do NOT force his job, business, money, clients, or "closing" into a line. Do NOT rewrite a clean joke just to make it "different." Same format, same subject — surgical edits only, where genuinely needed.

Keep the count and order. Return ONLY JSON: {"captions": ["<adapted 1>", "<adapted 2>", ...]}"""


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


def bootstrap_from(target, source, limit: int = 40, reset: bool = False) -> int:
    """Reskin up to `limit` of the source profile's (non-gambling) refs into the target's voice. With
    reset=True the target's previous BOOTSTRAP refs are dropped first (real/ingested refs are kept);
    otherwise it appends. Returns how many were added."""
    from app.caption.engine import _is_gambling   # lazy: avoid pulling the engine at module import

    src = [r for r in load_refs(profiles.corpus_path(source))
           if (r.get("caption") or "").strip() and not _is_gambling(r)][:limit]
    tp = (profiles.read_persona(target) or "").strip() or "an actually-cool, confident, very-online creator"

    new_refs: list[dict] = []
    for i in range(0, len(src), 12):                 # chunk so each LLM call stays small + reliable
        chunk = src[i:i + 12]
        caps = reskin([r["caption"] for r in chunk], tp)
        for r, c in zip(chunk, caps):
            new_refs.append({"caption": c, "source": "bootstrap", "persona_trait": r.get("persona_trait", "core")})

    path = profiles.corpus_path(target)
    kept = load_refs(path)
    if reset:
        kept = [r for r in kept if r.get("source") != "bootstrap"]   # drop the old seed, keep real refs
    combined = kept + new_refs
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:     # rewrite + renumber (small file; avoids id clashes)
        for j, r in enumerate(combined):
            f.write(json.dumps({**r, "ref_id": f"r{j + 1:03d}"}, ensure_ascii=False) + "\n")
    return len(new_refs)
