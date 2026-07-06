"""One-off decode split for PROMOTED refs: move the current (long, analyst-depth) why_it_works
to why_full, then generate a short anchor-facing why_it_works by COMPRESSING why_full (never
re-deriving blind — the original operator-note insight is already folded into the existing
decode and isn't stored anywhere else). Adds the generativity label in the same call.

Seeds (source == "seed_verbatim") are untouched, byte-identical. Idempotent via "decode_v": 2.

Usage (local state):   .venv/bin/python scripts/regen_promoted_decodes.py [--write] [--root var/profiles]
Prod (Railway volume): POST /api/debug/regen-decodes {"write": true|false}

--dry-run is the DEFAULT: full compute (LLM calls included) + full report, ZERO mutation.
--write mutates, after a timestamped backup of each voice's references.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FORBIDDEN_RX = re.compile(r"operator|note|feedback|grade|rating|rated", re.IGNORECASE)
SEED_SOURCE = "seed_verbatim"

_COMPRESS_SYS = """You rewrite ONE caption-decode for a creator's reference corpus. You get the caption and the FULL analysis of why it works. Produce:

- why_it_works: ≤ 50 words, punchy, mechanism-first, in the creator's own blunt idiom — keep the mechanism insight fully intact, name the exact word/image/logic that snaps, cut every bit of analysis padding. It is read inside the creator's own voice prompt as self-knowledge: NEVER reference an operator, a note, feedback, a grade, or a rating.
- generativity: "generative" if the line's MECHANISM transposes to fresh subjects (a move that can be run again on new material); "singular" if it won on a one-time collision of premise/word/moment that doesn't meaningfully transpose. One judgment, no hedging.

Return ONLY JSON: {"why_it_works": "...", "generativity": "generative" | "singular"}"""


def _words(t: str) -> int:
    return len((t or "").split())


def _voice_owners(root: str) -> list[str]:
    """Voice-OWNER profile dirs, each exactly once: resolve every profile's voice.json pointer;
    process only dirs that are the resolved voice of at least one profile AND have a corpus."""
    owners: set[str] = set()
    for pid in sorted(os.listdir(root)):
        base = os.path.join(root, pid)
        if not os.path.isdir(base):
            continue
        target = pid
        vp = os.path.join(base, "voice.json")
        if os.path.exists(vp):
            try:
                with open(vp, encoding="utf-8") as f:
                    v = (json.load(f) or {}).get("voice_profile_id")
                if v:
                    target = str(v)
            except Exception:  # noqa: BLE001 — unreadable pointer -> own voice (fail-safe)
                pass
        owners.add(target)
    return [o for o in sorted(owners)
            if os.path.exists(os.path.join(root, o, "references.jsonl"))]


def _compress_one(caption: str, why_full: str) -> tuple[str | None, str | None, list[str]]:
    """One LLM call (one retry on validation failure). Returns (short_why, generativity, problems).
    short_why None => caller keeps the original text (why_full is preserved regardless)."""
    from app.caption.llm import complete_json
    problems: list[str] = []
    user = f"CAPTION:\n{caption}\n\nFULL ANALYSIS:\n{why_full}"
    gen = None
    for attempt in (1, 2):
        try:
            out = complete_json(_COMPRESS_SYS, user, effort="medium", max_tokens=800, tag="decode-split")
            s, e = out.find("{"), out.rfind("}")
            d = json.loads(out[s:e + 1]) if s != -1 else {}
        except Exception as ex:  # noqa: BLE001
            problems.append(f"attempt {attempt}: call/parse failed ({type(ex).__name__})")
            continue
        g = (d.get("generativity") or "").strip().lower()
        gen = gen or (g if g in ("generative", "singular") else None)
        short = (d.get("why_it_works") or "").strip()
        if not short:
            problems.append(f"attempt {attempt}: empty why_it_works")
            continue
        if _words(short) > 55:   # hard cap (prompt asks <=50)
            problems.append(f"attempt {attempt}: {_words(short)} words > 55")
            continue
        if FORBIDDEN_RX.search(short):
            problems.append(f"attempt {attempt}: forbidden reference in why_it_works")
            continue
        return short, gen, problems
    return None, gen, problems


def run_all(root: str = os.path.join("var", "profiles"), write: bool = False) -> dict:
    """Process every voice once. Full compute in dry-run; mutation only with write=True
    (timestamped backup first). Idempotent: decode_v==2 refs are skipped."""
    report: dict = {"write": write, "voices": [], "per_ref": [], "samples": [],
                    "generativity": {"generative": 0, "singular": 0},
                    "logs": []}
    before_lens: list[int] = []
    after_lens: list[int] = []
    for owner in _voice_owners(root):
        path = os.path.join(root, owner, "references.jsonl")
        with open(path, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        refs = [json.loads(line) for line in lines]
        todo = [r for r in refs
                if r.get("source") != SEED_SOURCE
                and r.get("decode_v") != 2
                and not r.get("why_full")
                and (r.get("why_it_works") or "").strip()]
        skipped_no_decode = [r.get("ref_id") for r in refs
                             if r.get("source") != SEED_SOURCE and r.get("decode_v") != 2
                             and not r.get("why_full") and not (r.get("why_it_works") or "").strip()]
        seeds = sum(1 for r in refs if r.get("source") == SEED_SOURCE)
        report["voices"].append({"voice": owner, "refs": len(refs), "seeds": seeds,
                                 "to_process": len(todo), "already_v2": sum(1 for r in refs if r.get("decode_v") == 2),
                                 "skipped_no_decode": skipped_no_decode})
        if skipped_no_decode:
            report["logs"].append(f"{owner}: no decode to split on {skipped_no_decode}")
        if not todo:
            continue
        changed = False
        for r in todo:
            old = (r.get("why_it_works") or "").strip()
            short, gen, problems = _compress_one((r.get("caption") or "").strip(), old)
            kept_original = short is None
            new_short = old if kept_original else short
            if gen is None:
                gen = "generative"   # status-quo-preserving default
                report["logs"].append(f"{owner}/{r.get('ref_id')}: invalid generativity -> default 'generative'")
            if kept_original:
                report["logs"].append(f"{owner}/{r.get('ref_id')}: compression failed "
                                      f"({'; '.join(problems)}) -> original kept as why_it_works")
            r["why_full"] = old            # nothing is destroyed
            r["why_it_works"] = new_short
            r["generativity"] = gen
            r["decode_v"] = 2
            changed = True
            report["generativity"][gen] += 1
            before_lens.append(_words(old))
            after_lens.append(_words(new_short))
            report["per_ref"].append({"voice": owner, "ref_id": r.get("ref_id"),
                                      "before_words": _words(old), "after_words": _words(new_short),
                                      "generativity": gen, "kept_original": kept_original})
            if len(report["samples"]) < 5 and not kept_original:
                report["samples"].append({"ref_id": r.get("ref_id"),
                                          "caption": (r.get("caption") or "")[:90],
                                          "before": old, "after": new_short})
        if write and changed:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, f"{path}.bak-{stamp}")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in refs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
            report["logs"].append(f"{owner}: WROTE {path} (backup .bak-{stamp})")
    n = len(before_lens)
    report["processed"] = n
    report["mean_before_words"] = round(sum(before_lens) / n, 1) if n else None
    report["mean_after_words"] = round(sum(after_lens) / n, 1) if n else None
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # persist the report next to the profiles root: the Railway edge 502s a ~60-LLM-call request
    # long before it finishes, so the HTTP response is NOT a reliable carrier for the report
    try:
        rp = os.path.join(os.path.dirname(root.rstrip("/\\")) or ".", "decode_regen_report.json")
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    except Exception as ex:  # noqa: BLE001 — the report must never sink the run
        report["logs"].append(f"report persist failed: {ex}")
    return report


def last_report(root: str = os.path.join("var", "profiles")) -> dict | None:
    rp = os.path.join(os.path.dirname(root.rstrip("/\\")) or ".", "decode_regen_report.json")
    if not os.path.exists(rp):
        return None
    with open(rp, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="mutate (default: dry-run — full compute, no writes)")
    ap.add_argument("--root", default=os.path.join("var", "profiles"))
    args = ap.parse_args()
    rep = run_all(root=args.root, write=args.write)
    print(json.dumps({k: v for k, v in rep.items() if k != "samples"}, ensure_ascii=False, indent=1))
    for s in rep["samples"]:
        print(f"\n--- {s['ref_id']} · {s['caption']}")
        print(f"BEFORE ({_words(s['before'])}w): {s['before']}")
        print(f"AFTER  ({_words(s['after'])}w): {s['after']}")
    print(f"\nmode={'WRITE' if args.write else 'DRY-RUN'} processed={rep['processed']} "
          f"mean {rep['mean_before_words']} -> {rep['mean_after_words']} words · "
          f"generativity {rep['generativity']}")
