"""Phase-1 fix-pass tests. Plain-assert runner (no pytest in the venv):
    .venv/bin/python tests/test_phase1.py
Monkeypatches module attributes directly; every patch is restored per-test."""
import contextlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_RESULTS = []


def test(fn):
    _RESULTS.append(fn)
    return fn


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


# ─── Task 1: codex validation + retry-preserves-previous ───

GOOD_CODEX = "\n\n".join(
    f"## {i}. {name}\n" + ("His lines work because the mechanism is real and the reader feels "
                           "the turn land in one beat, every single time, no padding anywhere.")
    for i, name in enumerate(["THE CORE", "THE CRAFT", "THE FORM", "THE TEXTURE",
                              "THE TRIPWIRES", "EIGHT VS TEN"], 1))

TRUNCATED_CODEX = GOOD_CODEX.split("## 6.")[0].rstrip()[:-40] + " if you had to explain it — you"


@test
def codex_validator_accepts_good():
    from app.caption.lab import validate_codex
    assert validate_codex(GOOD_CODEX) == [], validate_codex(GOOD_CODEX)
    # emoji / closing quote directly after a sentence terminal is fine
    assert validate_codex(GOOD_CODEX + " It ends like this.”") == []
    assert validate_codex(GOOD_CODEX + " It snaps. \U0001f480") == []


@test
def codex_validator_flags_truncation():
    from app.caption.lab import validate_codex
    fails = validate_codex(TRUNCATED_CODEX)
    assert any("EIGHT VS TEN" in f for f in fails), fails
    assert any("mid-sentence" in f for f in fails), fails
    # comma / colon / dash endings are mid-sentence
    assert validate_codex(GOOD_CODEX + " and then,") != []
    assert validate_codex(GOOD_CODEX + " namely:") != []
    assert validate_codex(GOOD_CODEX + " because —") != []
    assert validate_codex("") == ["empty codex"]


@test
def codex_retry_then_keeps_previous():
    from app.caption import lab
    import app.corpus.reels as reels_mod
    import app.corpus.store as store_mod
    calls = {"n": 0}

    def bad_llm(*a, **k):
        calls["n"] += 1
        return TRUNCATED_CODEX

    with tempfile.TemporaryDirectory() as td:
        codex_path = os.path.join(td, "lab_codex.md")
        with open(codex_path, "w", encoding="utf-8") as f:
            f.write("PREVIOUS GOOD CODEX")
        with patched(lab, complete_json=bad_llm, persona=lambda: "P",
                     _codex_path=lambda: codex_path), \
             patched(reels_mod, graded=lambda *a, **k: []), \
             patched(store_mod, load_refs=lambda *a, **k: []):
            out = lab.build_codex(force=True)
        assert calls["n"] == 3, f"expected 3 attempts, got {calls['n']}"
        assert out.get("ok") is False and out.get("rebuilt") is False, out
        assert out.get("failures"), out
        assert out.get("codex") == "PREVIOUS GOOD CODEX"
        with open(codex_path, encoding="utf-8") as f:
            assert f.read() == "PREVIOUS GOOD CODEX", "previous codex was overwritten"


@test
def codex_valid_build_writes():
    from app.caption import lab
    import app.corpus.reels as reels_mod
    import app.corpus.store as store_mod
    with tempfile.TemporaryDirectory() as td:
        codex_path = os.path.join(td, "lab_codex.md")
        with patched(lab, complete_json=lambda *a, **k: GOOD_CODEX, persona=lambda: "P",
                     _codex_path=lambda: codex_path), \
             patched(reels_mod, graded=lambda *a, **k: []), \
             patched(store_mod, load_refs=lambda *a, **k: []):
            out = lab.build_codex(force=True)
        assert out.get("ok") is True and out.get("rebuilt") is True, out
        with open(codex_path, encoding="utf-8") as f:
            assert f.read() == GOOD_CODEX


# ─── Task 2: echo-based anchor attribution in the batch path ───

def _run_generate(model_json: str, n: int = 3):
    """Run engine.generate() with everything except the parsing logic stubbed out."""
    from app.caption import engine
    anchors = [{"ref_id": f"A{i}", "caption": f"anchor {i}", "why_it_works": None} for i in range(n)]
    with patched(engine,
                 load_refs=lambda *a, **k: list(anchors),
                 _pick_anchors=lambda refs, k: anchors[:k],
                 _avoid_block=lambda *a, **k: "(none yet)",
                 persona=lambda: "P",
                 refine=lambda cands: cands,
                 _drop_ref_copies=lambda cands: cands,
                 _coherence_gate=lambda cands: cands,
                 log_generated=lambda texts: None,
                 complete_json=lambda *a, **k: model_json):
        return engine.generate(n=n)


@test
def echo_correct_attribution():
    out = _run_generate(json.dumps({"candidates": [
        {"anchor": 0, "text": "t0"}, {"anchor": 1, "text": "t1"}, {"anchor": 2, "text": "t2"}]}))
    assert [c["text"] for c in out] == ["t0", "t1", "t2"]
    assert [c["anchor_ref"] for c in out] == ["A0", "A1", "A2"]
    assert all(c["anchor_refs"] == [c["anchor_ref"]] for c in out)
    assert all("anchor" not in c for c in out), "echo index must not leak into output"
    assert all(c.get("caption_id") for c in out)


@test
def echo_reordered_response_attributes_by_echo():
    out = _run_generate(json.dumps({"candidates": [
        {"anchor": 2, "text": "t2"}, {"anchor": 0, "text": "t0"}, {"anchor": 1, "text": "t1"}]}))
    got = {c["text"]: c["anchor_ref"] for c in out}
    assert got == {"t2": "A2", "t0": "A0", "t1": "A1"}, got


@test
def echo_duplicate_and_out_of_range_dropped():
    out = _run_generate(json.dumps({"candidates": [
        {"anchor": 0, "text": "keep"}, {"anchor": 0, "text": "dupe"},
        {"anchor": 9, "text": "oob"}, {"anchor": True, "text": "bool"},
        {"anchor": -1, "text": "neg"}]}), n=5)
    assert [(c["text"], c["anchor_ref"]) for c in out] == [("keep", "A0")], out


@test
def echo_legacy_response_drops_never_positional():
    out = _run_generate(json.dumps({"candidates": [{"text": "x"}, {"text": "y"}, {"text": "z"}]}))
    assert out == [], f"legacy no-echo response must drop, not positionally attribute: {out}"


if __name__ == "__main__":
    failed = 0
    for fn in _RESULTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}", flush=True)
        except AssertionError as ex:
            failed += 1
            print(f"FAIL  {fn.__name__}: {ex}", flush=True)
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(ex).__name__}: {ex}", flush=True)
    print(f"\n{len(_RESULTS) - failed}/{len(_RESULTS)} passed", flush=True)
    sys.exit(1 if failed else 0)
