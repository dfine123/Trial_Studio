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
    """Run engine.generate() V1 path with everything except the parsing logic stubbed out."""
    from app.caption import engine
    from app.config import settings
    anchors = [{"ref_id": f"A{i}", "caption": f"anchor {i}", "why_it_works": None} for i in range(n)]
    with patched(settings, generation_engine="v1"), \
         patched(engine,
                 load_refs=lambda *a, **k: list(anchors),
                 _pick_anchors=lambda refs, k, **kw: anchors[:k],
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


# ─── Task 4: decode-split regen script ───

def _fake_voice_dir(td: str) -> str:
    root = os.path.join(td, "profiles")
    vdir = os.path.join(root, "voice-a")
    os.makedirs(vdir)
    refs = [
        {"ref_id": "r001", "source": "seed_verbatim", "caption": "seed one", "why_it_works": "short seed decode"},
        {"ref_id": "r002", "source": "seed_verbatim", "caption": "seed two", "why_it_works": None},
        {"ref_id": "p001", "source": "promoted_gen", "caption": "promoted one",
         "why_it_works": " ".join(["analysis"] * 100)},
        {"ref_id": "p002", "source": "note_endorsed", "caption": "promoted two",
         "why_it_works": " ".join(["essay"] * 110)},
    ]
    with open(os.path.join(vdir, "references.jsonl"), "w", encoding="utf-8") as f:
        for r in refs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # a second profile POINTING at voice-a (must not cause double-processing)
    pdir = os.path.join(root, "pointer-b")
    os.makedirs(pdir)
    with open(os.path.join(pdir, "voice.json"), "w", encoding="utf-8") as f:
        json.dump({"voice_profile_id": "voice-a"}, f)
    return root


GOOD_SPLIT = json.dumps({"why_it_works": "the snap is the exact word — short and blunt",
                         "generativity": "singular"})


@test
def regen_dry_run_mutates_nothing():
    from scripts import regen_promoted_decodes as rg
    with tempfile.TemporaryDirectory() as td:
        root = _fake_voice_dir(td)
        path = os.path.join(root, "voice-a", "references.jsonl")
        before = open(path, encoding="utf-8").read()
        with patched(rg, _compress_one=lambda cap, why: ("short version", "generative", [])):
            rep = rg.run_all(root=root, write=False)
        assert rep["processed"] == 2, rep
        assert open(path, encoding="utf-8").read() == before, "dry-run wrote to disk"
        assert not [f for f in os.listdir(os.path.join(root, "voice-a")) if ".bak-" in f]


@test
def regen_write_splits_and_is_idempotent():
    from scripts import regen_promoted_decodes as rg
    with tempfile.TemporaryDirectory() as td:
        root = _fake_voice_dir(td)
        path = os.path.join(root, "voice-a", "references.jsonl")
        seed_lines_before = [line for line in open(path, encoding="utf-8")
                             if '"seed_verbatim"' in line]
        with patched(rg, _compress_one=lambda cap, why: ("short version", "singular", [])):
            rep = rg.run_all(root=root, write=True)
        assert rep["processed"] == 2 and rep["generativity"]["singular"] == 2, rep
        refs = {json.loads(line)["ref_id"]: json.loads(line) for line in open(path, encoding="utf-8")}
        p1 = refs["p001"]
        assert p1["why_full"].startswith("analysis") and p1["why_it_works"] == "short version"
        assert p1["decode_v"] == 2 and p1["generativity"] == "singular"
        seed_lines_after = [line for line in open(path, encoding="utf-8") if '"seed_verbatim"' in line]
        assert seed_lines_after == seed_lines_before, "seeds not byte-identical"
        assert [f for f in os.listdir(os.path.join(root, "voice-a")) if ".bak-" in f], "no backup"
        # voice-a processed exactly once despite pointer-b pointing at it
        assert [v["voice"] for v in rep["voices"]] == ["voice-a"], rep["voices"]
        # idempotent: second run is a no-op
        content = open(path, encoding="utf-8").read()
        with patched(rg, _compress_one=lambda cap, why: ("DIFFERENT", "generative", [])):
            rep2 = rg.run_all(root=root, write=True)
        assert rep2["processed"] == 0, rep2
        assert open(path, encoding="utf-8").read() == content, "re-run mutated the file"


@test
def regen_failed_compression_keeps_original():
    from scripts import regen_promoted_decodes as rg
    with tempfile.TemporaryDirectory() as td:
        root = _fake_voice_dir(td)
        path = os.path.join(root, "voice-a", "references.jsonl")
        with patched(rg, _compress_one=lambda cap, why: (None, None, ["forbidden reference"])):
            rep = rg.run_all(root=root, write=True)
        refs = {json.loads(line)["ref_id"]: json.loads(line) for line in open(path, encoding="utf-8")}
        p1 = refs["p001"]
        assert p1["why_it_works"] == p1["why_full"], "original text must be kept on failure"
        assert p1["generativity"] == "generative", "default must be status-quo-preserving"
        assert all(e["kept_original"] for e in rep["per_ref"]), rep["per_ref"]


@test
def regen_validator_rejects_bad_outputs():
    from scripts import regen_promoted_decodes as rg
    calls = {"n": 0}
    responses = [json.dumps({"why_it_works": " ".join(["w"] * 60), "generativity": "generative"}),
                 GOOD_SPLIT]

    def fake_llm(system, user, **k):
        out = responses[min(calls["n"], 1)]
        calls["n"] += 1
        return out

    import app.caption.llm as llm_mod
    with patched(llm_mod, complete_json=fake_llm):
        # _compress_one imports complete_json lazily from app.caption.llm
        short, gen, problems = rg._compress_one("cap", "full analysis")
    assert short == "the snap is the exact word — short and blunt", (short, problems)
    assert gen in ("generative", "singular")
    assert any("> 55" in p for p in problems), problems


# ─── Produce-mode anchor selection (reel-slate quality fix) ───

def _mk_ref(rid, trait, caption="a line", source=None):
    r = {"ref_id": rid, "persona_trait": trait, "caption": caption}
    if source:
        r["source"] = source
    return r


def _pick(refs, n, produce, offsets=None, usage=None):
    from app.caption import engine
    with patched(engine,
                 _load_json=lambda path: dict(usage or {}) if "usage" in str(path) else {},
                 _save_ref_usage=lambda u: None,
                 _quality_offsets=(lambda healthy: dict(offsets or {}))):
        return engine._pick_anchors(list(refs), n, produce=produce)


@test
def produce_offsets_reorder_strong_first():
    refs = [_mk_ref(f"r{i}", f"t{i}") for i in range(8)]
    offsets = {"r0": 4, "r1": 4, "r2": 4}          # weak-history refs delayed
    got = {a["ref_id"] for a in _pick(refs, 5, produce=True, offsets=offsets)}
    assert got == {"r3", "r4", "r5", "r6", "r7"}, got


@test
def produce_never_excludes_weak_refs():
    refs = [_mk_ref(f"r{i}", f"t{i}") for i in range(6)]
    offsets = {"r0": 4}
    usage = {f"r{i}": 10 for i in range(1, 6)}      # others heavily used — weak ref's turn comes
    got = {a["ref_id"] for a in _pick(refs, 3, produce=True, offsets=offsets, usage=usage)}
    assert "r0" in got, got


@test
def explore_mode_ignores_quality_offsets():
    from app.caption import engine
    refs = [_mk_ref(f"r{i}", f"t{i}") for i in range(6)]

    def boom(healthy):
        raise AssertionError("explore mode must not compute quality offsets")

    with patched(engine, _load_json=lambda path: {}, _save_ref_usage=lambda u: None,
                 _quality_offsets=boom):
        got = engine._pick_anchors(list(refs), 4, produce=False)
    assert len(got) == 4


@test
def species_floor_holds_in_both_modes_and_is_quality_ordered():
    # 6 plain refs + 2 frame refs; frames carry max usage so plain rotation never reaches them
    refs = [_mk_ref(f"r{i}", f"t{i}") for i in range(6)]
    refs.append(_mk_ref("frA", "frameyA", caption="when the frame lands"))
    refs.append(_mk_ref("frB", "frameyB", caption="when the other frame lands"))
    refs.append(_mk_ref("si", "deep_bro_sincere"))
    usage = {"frA": 99, "frB": 99, "si": 99}
    exp = {a["ref_id"] for a in _pick(refs, 5, produce=False, usage=usage)}
    assert ("frA" in exp or "frB" in exp) and "si" in exp, f"explore must floor species in: {exp}"
    # produce mode ALSO floors species (adversary: sincere is the top-performing posted species),
    # and picks the offset-favored ref WITHIN the species
    prod = {a["ref_id"] for a in _pick(refs, 5, produce=True, usage=usage,
                                       offsets={"frA": 3, "frB": -3})}
    assert "frB" in prod and "frA" not in prod and "si" in prod, prod


@test
def winner_reserve_era_fix():
    from app.caption import engine
    refs = [_mk_ref(f"r{i}", f"t{i}") for i in range(8)]
    # r7 has 2 keeps (two >=8 posted reels) and max usage — the reserve must still surface it
    scores = {"r7": {"keep": 2, "kill": 0, "best": 0}}
    with patched(engine,
                 _load_json=lambda path: dict(scores) if "scores" in str(path) else {"r7": 99},
                 _save_ref_usage=lambda u: None):
        got = {a["ref_id"] for a in engine._pick_anchors(list(refs), 4, produce=False)}
    assert "r7" in got, f"revived winner reserve must include r7: {got}"


@test
def quality_offsets_math():
    from app.caption import engine
    import app.corpus.reels as reels_mod
    strong = _mk_ref("S", "t1")                                   # rated 9,9,9
    weak = _mk_ref("W", "t2")                                     # rated 2,2,2
    validated = _mk_ref("V", "t3", source="promoted_gen")         # no reels: NO provenance boost
    fresh = _mk_ref("F", "t4")                                    # no data at all
    rehab = _mk_ref("R", "t5")                                    # old 2s + batch keeps heal it
    fakes = ([{"grade": {"rating": 9}, "caption_anchor_refs": ["S"]}] * 3
             + [{"grade": {"rating": 2}, "caption_anchor_refs": ["W"]}] * 3
             + [{"grade": {"rating": 2}, "caption_anchor_refs": ["R"]}] * 2
             + [{"grade": {"rating": 5}, "caption_anchor_refs": ["X"]}] * 20)
    with patched(reels_mod, graded=lambda *a, **k: list(fakes)), \
         patched(engine, _load_json=lambda path: {"R": {"keep": 4, "kill": 0}} if "scores" in str(path) else {}):
        off = engine._quality_offsets([strong, weak, validated, fresh, rehab])
    assert off["S"] < 0, off
    assert off["W"] > 0, off
    assert off["V"] == 0, f"provenance must NOT boost (measured anti-signal): {off}"
    assert off["F"] == 0, off
    assert off["R"] > off["S"] and off["R"] < off["W"], f"batch keeps must soften the penalty: {off}"
    assert all(-3 <= v <= 3 for v in off.values()), off
    with patched(reels_mod, graded=lambda *a, **k: fakes[:10]), \
         patched(engine, _load_json=lambda path: {}):             # <20 ratings -> neutral
        assert engine._quality_offsets([strong, weak]) == {}


# ─── Morph guard: noun-swaps of catalog refs drop; frame species survive ───

@test
def morph_guard_drops_noun_swaps_keeps_frames():
    from app.caption import engine
    refs = [
        {"caption": "Raccoons don't got a resume, a degree, or a plan. and they eating everywhere"},
        {"caption": "would you rather $20 right now or 3 billion dollars but you gotta wait for the microwave"},
    ]
    cands = [
        {"text": "Seagulls don't got a resume, a degree, or a plan. and they eating on every boardwalk"},
        {"text": "would you rather $50 right now or 8 billion dollars but every song cuts out on the last note"},
        {"text": "told her i work in last-mile logistics (i deliver mcdonald's on a rented lime scooter)"},
    ]
    with patched(engine, load_refs=lambda *a, **k: list(refs)):
        kept = engine._drop_ref_copies(list(cands))
    texts = [c["text"] for c in kept]
    assert not any("Seagulls" in t for t in texts), f"noun-swap must drop: {texts}"
    assert any("every song cuts out" in t for t in texts), f"fresh wyr must survive: {texts}"
    assert any("lime scooter" in t for t in texts), texts


# ─── Generation v2: understanding-first two-stage (production default) ───

@test
def v2_two_stage_generates_without_anchors():
    from app.caption import engine, lab
    from app.config import settings
    calls = []

    def fake_llm(system, user, **kw):
        calls.append(kw.get("tag"))
        if kw.get("tag") == "ideate":
            return json.dumps({"ideas": [{"premise": f"premise {i}", "play": f"play {i}",
                                          "charge": "collapse"} for i in range(8)]})
        return json.dumps({"captions": [f"caption {i}" for i in range(5)]})

    refs = [{"ref_id": "r1", "caption": "a real catalog line", "why_it_works": "w"}]
    with patched(settings, generation_engine="v2"), \
         patched(lab, build_codex=lambda force=False: {"codex": "THE CODEX TEXT"}), \
         patched(engine,
                 load_refs=lambda *a, **k: list(refs),
                 _avoid_block=lambda *a, **k: "(none yet)",
                 persona=lambda: "P",
                 refine=lambda cands: cands,
                 _drop_ref_copies=lambda cands: cands,
                 _coherence_gate=lambda cands: cands,
                 log_generated=lambda texts: None,
                 complete_json=fake_llm):
        out = engine.generate(n=5)
    assert calls == ["ideate", "batch-captions"], calls
    assert len(out) == 5, out
    assert all(c["anchor_refs"] == [] and c["anchor_ref"] is None for c in out), out
    assert all(c.get("caption_id") for c in out)
    # the reel path routes through the same v2 core
    with patched(settings, generation_engine="v2"), \
         patched(lab, build_codex=lambda force=False: {"codex": "X"}), \
         patched(engine, load_refs=lambda *a, **k: list(refs),
                 _avoid_block=lambda *a, **k: "(none yet)", persona=lambda: "P",
                 refine=lambda cands: cands, _drop_ref_copies=lambda cands: cands,
                 _coherence_gate=lambda cands: cands, log_generated=lambda texts: None,
                 complete_json=fake_llm):
        out2 = engine.generate_independent(k=5)
    assert len(out2) == 5 and all(not c["anchor_refs"] for c in out2)


# ─── Heal-on-ingest: AI-generated low-spec clips normalize instead of rejecting ───

def _make_clip(td, w, h, fps, dur=4.5):
    import subprocess
    out = os.path.join(td, f"synth_{w}x{h}_{fps}.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size={w}x{h}:rate={fps}:duration={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                   check=True, capture_output=True)
    return out


@test
def heal_normalizes_ai_spec_clips():
    import shutil as _sh
    if not _sh.which("ffmpeg"):
        print("  (skipped: no ffmpeg)")
        return
    from app.indexing import qc
    from app.indexing.pipeline import _normalize_for_ingest
    with tempfile.TemporaryDirectory() as td:
        # 16fps 640x360 — classic AI-generator output; must fail QC, then heal
        src = _make_clip(td, 640, 360, 16)
        res = qc.check(src, 720, 23.0)
        assert not res.passed, "16fps/360p must fail the raw gate"
        healed = _normalize_for_ingest(src, res.probe, 720, 23.0)
        assert healed and os.path.exists(healed), "heal produced nothing"
        res2 = qc.check(healed, 720, 23.0)
        assert res2.passed, res2.reason
        assert min(res2.probe.width, res2.probe.height) >= 720
        assert res2.probe.fps >= 23.0
        # a clip that's already fine must NOT be touched
        good = _make_clip(td, 720, 1280, 30)
        gres = qc.check(good, 720, 23.0)
        assert gres.passed
        assert _normalize_for_ingest(good, gres.probe, 720, 23.0) is None


# ─── Chooser judge model (taste-inversion fix) ───

@test
def chooser_uses_configured_judge_model():
    from app.caption import chooser
    got = {}

    def fake_llm(system, user, **kw):
        got.update(kw)
        return '{"best": 1}'

    with patched(chooser, complete_json=fake_llm, _system=lambda: "SYS"):
        pick = chooser.choose_best(["a", "b", "c"])
    from app.config import settings
    assert got.get("model") == settings.chooser_model, got
    assert pick == "b"


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
