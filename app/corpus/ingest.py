"""Automated reference ingestion — a folder of post screenshots -> labeled corpus records.

Runs Claude vision with the SAME labeling we converged on by hand, so the user can drop a
folder of screenshots and scale the corpus past 5-at-a-time. Dedups by caption text.

    python -m app.corpus.ingest corpus/inbox
"""
from __future__ import annotations

import base64
import glob
import json
import os
import sys

from anthropic import Anthropic

from app.config import settings
from app.corpus.store import CORPUS_PATH, load_refs

_LABEL_SYS = """You are cataloguing a short-form post (a screenshot) into a caption corpus used to train a caption engine in this creator's voice. Read the on-screen caption verbatim and analyze WHY it works.

Rules learned the hard way:
- DECODE the actual mechanism (e.g. "unborn kids eaten alive" is an IYKYK oral-sex innuendo, NOT "dark"). Explain why it really lands, not the surface structure.
- Identify the ONE primary lever — shareability is usually dominant ("who would you send this to") — plus any secondary levers.
- persona: "core_persona" if it works for ANY creator/theme (most do), else "theme_specific". persona_trait = the mode, e.g. shameless_villain, anti_simp, deep_bro_sincere, ego_wordplay_villain, anticope_callout, absurd_villain, self_aware_hustler, deadpan_crude, antimediocrity_dread, antideep_parody, self_aware_absurd_flex, backhanded_deadpan — or a new precise label if none fit.
- The clip shown is INCIDENTAL unless the caption REQUIRES a specific shot (e.g. a "how I look at X after Y" reaction needs a candid look-to-camera). clip_dependency: none | soft | intrinsic.
- Capture visible engagement metrics (views/likes/comments) if shown — strongest signal.
- format: single | progression (before/after).

Return ONLY JSON, no prose:
{"caption":"verbatim incl. emojis","why_it_works":"decoded, specific","primary_lever":"...","secondary_levers":["..."],"persona":"core_persona|theme_specific","persona_trait":"...","format":"single","clip_dependency":"none|soft|intrinsic","clip_note":"only if soft/intrinsic","metrics":null,"notes":"..."}"""

_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def label_image(path: str) -> dict:
    media_type = _MEDIA.get(os.path.splitext(path)[1].lower(), "image/jpeg")
    msg = Anthropic(api_key=settings.anthropic_api_key).messages.create(
        model=settings.caption_model,
        max_tokens=1500,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=_LABEL_SYS,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": _b64(path)}},
            {"type": "text", "text": "Catalogue this post."},
        ]}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


def ingest_folder(folder: str, append: bool = True) -> list[dict]:
    paths = sorted(p for ext in _MEDIA for p in glob.glob(os.path.join(folder, f"*{ext}")))
    if not paths:
        print(f"no images in {folder}")
        return []

    existing = {r.get("caption") for r in load_refs()}
    next_id = len(load_refs()) + 1
    new: list[dict] = []
    for p in paths:
        try:
            rec = label_image(p)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {os.path.basename(p)}: {exc}")
            continue
        if rec.get("caption") in existing:
            print(f"  skip dup: {(rec.get('caption') or '')[:45]}")
            continue
        rec["ref_id"] = f"r{next_id:03d}"
        rec.setdefault("source", "screenshot_auto")
        existing.add(rec.get("caption"))
        new.append(rec)
        next_id += 1
        print(f"  + {rec['ref_id']} [{rec.get('persona_trait')}] {(rec.get('caption') or '')[:50]}")

    if append and new:
        with open(CORPUS_PATH, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\ningested {len(new)} new ({len(paths)} images seen)")
    return new


if __name__ == "__main__":
    ingest_folder(sys.argv[1] if len(sys.argv) > 1 else "corpus/inbox")
