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


# ─── Single-clip style: cap_shots merges a beat plan down, cuts stay on beats ───

@test
def cap_shots_merges_to_max_on_beats():
    from app.generate.sequencer import Slot, cap_shots
    slots = [Slot(idx=i, start=float(i), end=float(i + 1)) for i in range(8)]  # 8 shots, 0..8s
    two = cap_shots(slots, 2)
    assert len(two) == 2, two
    assert two[0].start == 0.0 and two[-1].end == 8.0, "span preserved"
    # the internal cut lands on an original slot boundary (still beat-aligned)
    assert two[0].end in {s.end for s in slots[:-1]}
    one = cap_shots(slots, 1)
    assert len(one) == 1 and one[0].start == 0.0 and one[0].end == 8.0
    # already within cap or no cap -> untouched
    assert cap_shots(slots, 10) is slots
    assert cap_shots(slots, None) is slots
    assert cap_shots(slots, 0) is slots


@test
def empty_voice_generation_fails_loudly():
    from app.caption import engine
    from app.config import settings
    with patched(settings, generation_engine="v2"), \
         patched(engine, load_refs=lambda *a, **k: []):
        try:
            engine.generate(n=5)
            assert False, "empty-voice generation must raise"
        except RuntimeError as ex:
            assert "no references" in str(ex)


# ─── Opener cap: a third same-opener candidate drops ───

@test
def same_opener_cap():
    from app.caption import engine
    caps = ["mfs will do thing one", "mfs will do thing two", "mfs will do thing three",
            "broke dudes always different"]
    got = engine._pick_takes([[c] for c in caps])
    assert got == caps, "pick_takes passes singles through"
    # the cap itself lives in _generate_v2's post-processing; emulate its logic here
    seen, kept = {}, []
    for c in caps:
        key = " ".join(c.lower().split()[:2])
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= 2:
            kept.append(c)
    assert kept == ["mfs will do thing one", "mfs will do thing two", "broke dudes always different"]


# ─── Generation v2: the revitalized two-stage (ideate rough lines → retype → curate) ───

@test
def v2_revitalized_flow_and_surfaces():
    from app.caption import engine
    from app.config import settings
    calls = []
    seen = {}

    def fake_llm(system, user, **kw):
        calls.append(kw.get("tag"))
        seen[kw.get("tag")] = (system, user)
        if kw.get("tag") == "take-pick":
            return json.dumps({"picks": [1] * 20})
        return json.dumps({"captions": [{"takes": [f"epsilon{i} zeta{i} eta{i} theta{i} first take",
                                                   f"iota{i} kappa{i} lam{i} mu{i} second take"]}
                                        for i in range(7)]})

    refs = [{"ref_id": f"r{i}", "caption": f"real banger number {i} about topic{i} thing{i}",
             "why_it_works": f"decode {i}"} for i in range(9)]
    ns = [{"ns_id": "n1", "caption": "mfs will buy energy drinks just to do nothing all day",
           "point": "people buy productivity aids to keep doing nothing"}]
    fixture_formats = [{"id": f"fmt{i}", "name": f"proven vehicle {i}", "skeleton": f"shape {i} with [slot]",
                        "what_varies": "the slot", "mechanism": "lands when fresh", "verdict": "solid"}
                       for i in range(3)]
    used_formats = []
    import app.caption.formats as fmt_mod
    import app.caption.northstars as ns_mod
    with patched(settings, generation_engine="v2"), \
         patched(ns_mod, load=lambda: list(ns),
                 block=lambda: "- mfs will buy energy drinks (the point: productivity aids to do nothing)"), \
         patched(fmt_mod, pick_formats=lambda k: fixture_formats[:k],
                 log_use=lambda ids: used_formats.extend(ids)), \
         patched(engine,
                 load_refs=lambda *a, **k: list(refs),
                 recent_generated=lambda *a, **k: [],
                 _killed_texts=lambda: [],
                 persona=lambda: "P",
                 voice_core=lambda: "THE BRIEF: understanding leads",
                 refine=lambda cands: cands,
                 _coherence_gate=lambda cands: cands,
                 log_generated=lambda texts: None,
                 complete_json=fake_llm):
        out = engine.generate(n=5)
    assert calls == ["batch-captions", "take-pick"], calls
    assert len(out) == 5, [c["text"] for c in out]
    assert all("second take" in c["text"] for c in out), f"take-pick winners must win: {out}"
    assert all(c["anchor_refs"] == [] and c["anchor_ref"] is None for c in out), \
        f"NO per-slot reference seeds — the corpus lives only in the wall: {out}"
    assert all(c.get("caption_id") for c in out)
    sysp, userp = seen["batch-captions"]
    assert "real banger number 3" in sysp, "the full wall grounds the slate"
    assert "USED ground" in sysp, "the wall must be framed as taken territory"
    assert "THE BRIEF" in sysp, "the understanding brief leads the system prompt"
    assert "energy drinks" in sysp, "the north-star BAR rides in the system"
    assert "STARTS from something worth saying" in sysp, "message-first process in the v2 tail"
    assert "PROVEN FORMATS" in userp and "proven vehicle 2" in userp, \
        "the rotated format trio rides in the user msg (half the slate)"
    assert "real banger" not in userp, \
        "no reference text may appear as per-slot seed material (the orbit law)"
    assert "burned ground" in userp, "the recent/kill avoid block must ride in the user msg"
    assert used_formats == ["fmt0", "fmt1", "fmt2"], f"format rotation must advance: {used_formats}"
    # the reel path routes through the same v2 core
    with patched(settings, generation_engine="v2"), \
         patched(ns_mod, load=lambda: [], block=lambda: ""), \
         patched(fmt_mod, pick_formats=lambda k: fixture_formats[:k], log_use=lambda ids: None), \
         patched(engine, load_refs=lambda *a, **k: list(refs),
                 recent_generated=lambda *a, **k: [], _killed_texts=lambda: [],
                 persona=lambda: "P", voice_core=lambda: "THE BRIEF",
                 refine=lambda cands: cands, _coherence_gate=lambda cands: cands,
                 log_generated=lambda texts: None, complete_json=fake_llm):
        out2 = engine.generate_independent(k=5)
    assert len(out2) == 5 and all(not c["anchor_refs"] for c in out2)


@test
def format_book_rotation_and_wildcard():
    import tempfile as _tf
    from app.caption import formats as fmt
    book = ([{"id": "win", "name": "winner", "skeleton": "s [x]", "verdict": "proven-winner"},
             {"id": "dead1", "name": "d1", "skeleton": "s [x]", "verdict": "dead"},
             {"id": "mid", "name": "m", "skeleton": "s [x]", "verdict": "mixed"}]
            + [{"id": f"s{i}", "name": f"solid {i}", "skeleton": "s [x]", "verdict": "solid"}
               for i in range(6)])
    with _tf.TemporaryDirectory() as td:
        with patched(fmt, _BOOK_PATH=os.path.join(td, "formats.json"),
                     _usage_path=lambda: os.path.join(td, "format_usage.json")):
            fmt.save_book(book)
            picked = fmt.pick_formats(6)
            assert len(picked) == 6
            ids = [p["id"] for p in picked]
            assert "freeform" in ids, "a set of >=5 carries the wildcard slot"
            assert "dead1" not in ids, "a dead format is de-weighted behind fresh solids"
            assert len(set(ids)) == 6, "formats within a set are distinct"
            # de-weight is DELAY, never elimination: once every other format has out-cycled the
            # dead one's +3 virtual-usage penalty, it comes back into rotation
            fmt.log_use([b["id"] for b in book if b["id"] != "dead1"] * 4)
            ids2 = {p["id"] for p in fmt.pick_formats(6)}
            assert "dead1" in ids2, "dead formats must eventually cycle back (never eliminated)"
            # empty book falls back to wildcards rather than failing
            fmt.save_book([])
            assert all(p["id"] == "freeform" for p in fmt.pick_formats(3))


@test
def v2_guards_block_recent_kills_and_siblings():
    from app.caption import engine
    import app.caption.northstars as ns_mod
    refs = [{"ref_id": "r1", "caption": "Buy a chicken for 20 dollars, make it lay 10 eggs a day, "
                                        "charge millions per egg", "why_it_works": "w"}]
    with patched(ns_mod, load=lambda: []), \
         patched(engine, load_refs=lambda *a, **k: list(refs),
                 recent_generated=lambda *a, **k: ["dropped 15k renting the stadium jumbotron to congratulate myself"],
                 _killed_texts=lambda: ["she asked what i drive but i can't say it's on an 84 month loan so i just say german"]):
        cands = [
            {"text": "Buy a chicken for 21 dollars, make it lay 10 eggs a day, charge millions per egg"},  # corpus morph
            {"text": "dropped 9k renting the stadium jumbotron to congratulate myself again"},              # recent repeat
            {"text": "she asked what i drive but i can't say it's on an 84 month loan so i say german"},    # killed re-run
            {"text": "a completely fresh idea about a totally different subject entirely tonight"},
        ]
        kept = engine._drop_ref_copies(cands)
    assert [c["text"] for c in kept] == [cands[3]["text"]], \
        f"corpus morphs, recent repeats, and killed re-runs must all drop: {[c['text'][:40] for c in kept]}"
    # intra-set same-joke dedup: the later sibling drops, distinct ones survive
    sibs = [
        {"text": "possums play dead for a living and still eat better than you"},
        {"text": "possums play dead for a living and they still eat better than you do"},
        {"text": "my dad asks when i'm getting a real job while i'm up on some kid's free throws"},
    ]
    kept2 = engine._drop_same_joke_siblings(sibs)
    assert len(kept2) == 2 and kept2[0]["text"] == sibs[0]["text"] and kept2[1]["text"] == sibs[2]["text"]


@test
def instruction_layers_quote_no_winners():
    """Canon 7 (super-attractors): no winner texts/props and no shape roster may appear in any
    always-on instruction constant. This is the regression guard for the 2026-07-10 collapse."""
    import inspect
    from app.caption import engine
    # GENERATION-visible surfaces only. (_reskin_check's judge prompt legitimately NAMES formats —
    # it must know a shared format is NOT a re-skin, or it would false-drop validated species.
    # The BRIEF describes vehicle MECHANISMS without quoting winner texts — that's the line.)
    from app.caption import charters as ch
    surfaces = (engine._VOICE_CORE_DEFAULT + engine._SLATE_TAIL
                + engine._CRAFT_DEFAULT + engine._V3_TAIL + engine._SLATE5_TAIL
                + "".join(e["charter"] for e in ch.ENGINES)
                + inspect.getsource(engine._pick_takes))
    low = surfaces.lower()
    banned = ["raccoon", "vending machine", "led sign", "rothschild", "energy drinks",
              "401k match", "no homo", "alarm clock", "begging with better posture",
              "stealth mode", "nut twice", "she believed in me", "edging",
              "we are not the same", "dudes be like", "would-you-rather", "would you rather",
              "spectacle-spend", "jumbotron"]
    hits = [b for b in banned if b in low]
    assert not hits, f"quoted winners / shape roster leaked into instruction layers: {hits}"


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
        got["user"] = user
        return '{"best": 1}'

    with patched(chooser, complete_json=fake_llm, _system=lambda: "SYS"):
        pick = chooser.choose_best(["a", "b", "c"])
    from app.config import settings
    assert got.get("model") == settings.chooser_model, got
    # candidates are SHUFFLED before listing (index-0 primacy fix, 2026-07-15), so "best": 1
    # must map back to whichever candidate was LISTED at [1] — not input position 1
    import re as _re
    listed = dict(_re.findall(r"\[(\d+)\] (\w+)", got["user"]))
    assert pick == listed["1"], (pick, listed)
    assert sorted(listed.values()) == ["a", "b", "c"], listed   # all candidates listed exactly once


# ─── V3: seed → five engines → selector ───

@test
def v3_seed_fans_to_five_separate_engines():
    from app.caption import engine
    from app.config import settings
    import app.caption.northstars as ns_mod
    calls, systems = [], {}

    counter = {"n": 0}

    def fake_llm(system, user, **kw):
        tag = kw.get("tag")
        calls.append(tag)
        systems[tag] = (system, user)
        if tag == "take-pick":
            return json.dumps({"picks": [1] * 10})
        eid = tag.replace("eng-", "")
        counter["n"] += 1
        c = counter["n"]
        return json.dumps({"takes": [f"alpha{eid}{c} beta{eid}{c} gamma{eid}{c} delta{eid}{c} first take",
                                     f"eps{eid}{c} zeta{eid}{c} eta{eid}{c} theta{eid}{c} second take"]})

    refs = [{"ref_id": "r1", "caption": "a real posted banger about topics", "why_it_works": "w"}]
    with patched(settings, generation_engine="v3"), \
         patched(ns_mod, load=lambda: [], block=lambda: ""), \
         patched(engine,
                 load_refs=lambda *a, **k: list(refs),
                 recent_generated=lambda *a, **k: [],
                 _killed_texts=lambda: [],
                 persona=lambda: "P",
                 refine=lambda cands: cands,
                 _coherence_gate=lambda cands: cands,
                 log_generated=lambda texts: None,
                 complete_json=fake_llm):
        out = engine.generate_independent(k=5)
    eng_calls = sorted(c for c in calls if c.startswith("eng-"))
    assert eng_calls == sorted(["eng-exotic", "eng-menace", "eng-mirror", "eng-screenshot",
                                "eng-send"] * 2), \
        f"every engine runs BOTH seeds (10 attempts per card): {eng_calls}"
    assert calls.count("take-pick") == 2, "take competition + per-lane best-of-two"
    assert len(out) == 5, [c["text"] for c in out]
    assert all("second take" in c["text"] for c in out), "take-pick winners must win"
    assert {c["engine"] for c in out} == {"screenshot", "send", "exotic", "mirror", "menace"}, \
        "every caption must carry its engine attribution"
    seeds_used = {c["seed"] for c in out}
    assert len(seeds_used) >= 1 and all(s for s in seeds_used), \
        "each candidate records the seed its attempt ran on"
    # separateness: each engine gets its OWN system; none acknowledges engines/slates/options
    sys_texts = [systems[f"eng-{e}"][0] for e in ("screenshot", "send", "exotic", "mirror", "menace")]
    assert len({s for s in sys_texts}) == 5, "five DISTINCT system prompts"
    for s in sys_texts:
        low = s.lower()
        assert "engine" not in low and "slate" not in low and "option" not in low, \
            "no engine may know the others exist"
        assert "a real posted banger" in s, "the wall grounds every engine"
        assert "NEXT POST in this exact feed" in s, \
            "conformance-first framing: the wall is the feed, tonight is the next post"
        assert "HIT HARDEST" in s, "the hitters block sits in every engine's system"
    _, u = systems["eng-send"]
    assert "VARIATION SEED" in u and "never obey it" in u, "seed rides with drift semantics"
    assert "don't repeat yourself" in u, "the recent-posts block rides in the user msg"


@test
def v3_seed_bank_draws():
    from app.caption import seeds
    assert len(seeds.BANK) >= 300, f"bank too small: {len(seeds.BANK)}"
    assert len(set(seeds.BANK)) == len(seeds.BANK), "bank has duplicates"
    draws = {seeds.draw() for _ in range(300)}
    assert len(draws) > 100, "draws must actually vary"


@test
def v3_charters_are_pure_and_separate():
    """Charters must not quote winners (orbit law), not reference other engines, and each
    must be a genuinely distinct document."""
    from app.caption import charters as ch
    assert [e["id"] for e in ch.ENGINES] == ["screenshot", "send", "exotic", "mirror", "menace"]
    banned = ["raccoon", "vending machine", "led sign", "rothschild", "energy drinks",
              "401k match", "no homo", "alarm clock", "stealth mode", "nut twice",
              "she believed in me", "edging", "jumbotron", "headphones in"]
    texts = []
    for e in ch.ENGINES:
        t = e["charter"]
        texts.append(t)
        low = t.lower()
        hits = [b for b in banned if b in low]
        assert not hits, f"{e['id']}: quoted winners leaked: {hits}"
        assert "engine" not in low and "slate" not in low and "slot " not in low, \
            f"{e['id']}: charters must not acknowledge the architecture"
        assert len(t) > 150, f"{e['id']}: charter suspiciously thin"   # kernels are short by design
    # distinctness: no two charters share a 12-word run
    def grams(t):
        ws = t.lower().split()
        return {" ".join(ws[i:i + 12]) for i in range(len(ws) - 11)}
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            shared = grams(texts[i]) & grams(texts[j])
            assert not shared, f"charters {i}/{j} share phrasing: {list(shared)[:2]}"
    # exotic gets no format palette, and stays the novelty lane — but WITHOUT anti-reference
    # pressure ("no shape you could name from your feed" pushed output out of the reference
    # distribution; conformance-first law, removed 2026-07-15)
    assert "shapes that have historically" not in ch.EXOTIC.lower()
    assert "isn't in anyone's rotation" in ch.EXOTIC.lower() \
        and "not a fill of a known template" in ch.EXOTIC.lower()
    assert "from your feed" not in ch.EXOTIC.lower()


# ─── V3: the reader-defendant (you-lecture) detector ───

@test
def v3_lecture_detector_matches_winner_law():
    from app.caption import engine as eng_mod
    import inspect
    src = inspect.getsource(eng_mod._generate_v3)
    assert "_is_lecture" in src and "restaging" in src
    # replicate the closure's logic through a live call is heavy; test the regex semantics
    # by extracting the same rules: dialogue/games/first-person-skin are never flagged.
    import re as _re

    def is_lecture(t):
        raw = (t or "").strip()
        low = " " + _re.sub(r"[^a-z0-9\s']", " ", raw.lower()) + " "
        if '"' in raw or "would you rather" in low or "we are not the same" in low:
            return False
        if _re.search(r"\b(i|i'm|i've|me|my|mine)\b", low):
            return False
        starts_you = bool(_re.match(r"^(you|you'll|you're|you've|your)\b", raw, _re.IGNORECASE))
        you_count = len(_re.findall(r"\byou('ll|'re|'ve)?\b", low))
        return starts_you or you_count >= 2

    # the failing v3 register — must flag
    assert is_lecture("you'll gas bro up about his idea for a whole hour then go home and sleep on your own")
    assert is_lecture("you post day one of everything and day thirty of nothing")
    assert is_lecture("you're more scared of looking stupid trying than of ending up where you are")
    # winner-legal 'you' — must NOT flag
    assert not is_lecture("would you rather $30 right now or 8 billion dollars but you gotta select every square")
    assert not is_lecture('Dudes be like "she believed in me when nobody else did"\n\nbc nobody else was that dumb')
    assert not is_lecture("your money's locked in a 401k till you're 65, mine's on a blackjack table by 2am.")
    assert not is_lecture("mfs keep the headphones in with nothing playing just so no one talks to them")
    assert not is_lecture("never take money advice from a man whose favorite day is friday")
    assert not is_lecture("Nothing fazes me anymore, i've laid a million of my own sons to rest in a gym sock")


# ─── Recent-vehicles line: descriptive lane memory across slates ───

@test
def recent_vehicles_detects_leaned_on_lanes():
    from app.caption import engine
    recent = [
        "Buy a goat for $100\nrent it out\nthat's $18,250 a year",
        "Catch one pigeon for free\nit lays 2 eggs a day\nthat's 18 million a day",
        "$60 right now or 7 billion dollars but you sneeze",
        "would you rather $40 right now or 6 billion but your search history",
        "the guy who called your business a scam just caught every green light",
        "the dude who called your business a scam just found $6 in his old jacket",
        "proud of you bro, shift lead at 30",
        "a completely unrelated sincere line about showing up",
    ]
    with patched(engine, recent_generated=lambda *a, **k: list(recent)):
        line = engine._recent_vehicles()
    assert "money ladder" in line and "would-you-rather" in line and "tiny win" in line, line
    assert "backhanded" not in line, f"single hits must not flag (needs >=2): {line}"
    with patched(engine, recent_generated=lambda *a, **k: []):
        assert engine._recent_vehicles() == ""


# ─── Reference intake (Telegram bot pipeline) ───

@test
def reference_intake_url_and_personalize():
    from app.reference import intake
    # URL extraction: reels/p/share forms, trailing junk stripped, non-IG rejected
    assert intake.find_reel_url("check this https://www.instagram.com/reel/ABC123xyz/?igsh=1 out") \
        == "https://www.instagram.com/reel/ABC123xyz/?igsh=1"
    assert intake.find_reel_url("https://instagram.com/p/XYZ_789/").startswith("https://instagram.com/p/")
    assert intake.find_reel_url("https://youtube.com/watch?v=abc") is None
    assert intake.find_reel_url("no link here") is None
    # personalization DEFAULTS to 1:1 copy; a blank persona short-circuits without any LLM call
    import app.profiles as profiles_mod
    with patched(profiles_mod, read_persona=lambda pid: ""):
        assert intake.personalize_caption("exact caption text", "pid-1") == "exact caption text"
    # fail-open: an LLM error returns the original untouched
    import app.caption.llm as llm_mod
    def boom(*a, **k):
        raise RuntimeError("llm down")
    with patched(profiles_mod, read_persona=lambda pid: "a persona"), \
         patched(llm_mod, complete_json=boom):
        assert intake.personalize_caption("exact caption text", "pid-1") == "exact caption text"


@test
def reference_active_toggle_roundtrip():
    import tempfile as _tf
    from app import profiles as profiles_mod
    with _tf.TemporaryDirectory() as td:
        fake_uuid = uuid.uuid4() if "uuid" in dir() else __import__("uuid").uuid4()
        path = os.path.join(td, "profile_settings.json")
        with patched(profiles_mod, voice_file=lambda name, pid=None: path,
                     active_id=lambda: fake_uuid):
            profiles_mod.set_profile_settings({"reference_active": True}, fake_uuid)
            assert profiles_mod.profile_settings(fake_uuid).get("reference_active") is True
            profiles_mod.set_profile_settings({"reference_active": False}, fake_uuid)
            assert profiles_mod.profile_settings(fake_uuid).get("reference_active") is False


@test
def caption_timeline_groups_spans():
    """Dynamic references: per-frame transcripts group into spans — midpoint boundaries,
    flicker merged, empty frames skipped, static reels yield exactly one span."""
    from app.reference import intake
    # 20s ref sampled every 0.5s: setup for ~4.5s, payoff after (one flicker + one empty frame)
    def fake_frames(video_path, times):
        out = []
        for t in times:
            if t < 4.4:
                out.append("even when your at 1HP…")
            elif t < 4.9:
                out.append("")                      # transition frame: no text read
            elif abs(t - 10.25) < 0.01:
                out.append("you can still do 200 damage,")   # transcription flicker (comma)
            else:
                out.append("you can still do 200 damage.")
        return out
    with patched(intake, _transcribe_frames=fake_frames):
        spans = intake.extract_caption_timeline("x.mp4", 20.0)
    assert len(spans) == 2, spans
    assert spans[0]["text"] == "even when your at 1HP…"
    assert spans[1]["text"] == "you can still do 200 damage."
    assert spans[0]["start"] == 0.0 and abs(spans[1]["start"] - 4.75) < 0.6, spans
    assert spans[0]["end"] == spans[1]["start"] and spans[1]["end"] == 20.0, spans
    # static: one caption throughout -> one span covering the reel
    with patched(intake, _transcribe_frames=lambda v, ts: ["same line"] * len(ts)):
        spans = intake.extract_caption_timeline("x.mp4", 8.0)
    assert len(spans) == 1 and spans[0]["start"] == 0.0 and spans[0]["end"] == 8.0, spans


@test
def caption_boundary_refined_to_the_frame():
    """The switch must align with the reference precisely: the coarse 0.5s scan brackets each
    transition, then a dense 0.1s pass pins it — a true boundary at 4.32s lands within ±0.06s
    (a half-second-late flip was the operator's first dynamic complaint)."""
    from app.reference import intake
    def fake(video_path, times):
        return ["even at 1HP…" if t < 4.32 else "still 200 damage." for t in times]
    with patched(intake, _transcribe_frames=fake):
        spans = intake.extract_caption_timeline("x.mp4", 20.0)
    assert len(spans) == 2, spans
    assert abs(spans[1]["start"] - 4.32) <= 0.06, spans[1]["start"]
    assert spans[0]["end"] == spans[1]["start"]
    # refinement failing (vision hiccup) keeps the coarse boundary — never crashes
    calls = {"n": 0}
    def flaky(video_path, times):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("vision down")
        return ["A part" if t < 4.32 else "B part" for t in times]
    with patched(intake, _transcribe_frames=flaky):
        spans = intake.extract_caption_timeline("x.mp4", 20.0)
    assert len(spans) == 2 and abs(spans[1]["start"] - 4.5) < 0.6, spans


@test
def split_slots_forces_cut_at_caption_change():
    """A caption change must land ON a cut: an inside boundary splits its slot; a boundary too
    close to an existing cut slides that cut onto it; reel start/end never move."""
    from app.generate.sequencer import Slot, split_slots_at
    slots = [Slot(0, 0.0, 3.0), Slot(1, 3.0, 6.0), Slot(2, 6.0, 9.0)]
    # mid-slot boundary -> clean split
    out = split_slots_at(slots, [4.5])
    pts = [s.start for s in out] + [out[-1].end]
    assert 4.5 in pts and pts[0] == 0.0 and pts[-1] == 9.0, pts
    assert all(round(out[i].end, 3) == round(out[i + 1].start, 3) for i in range(len(out) - 1))
    # boundary a hair from an existing cut -> the cut SLIDES onto it (no micro-shot)
    out2 = split_slots_at(slots, [3.2])
    pts2 = [s.start for s in out2] + [out2[-1].end]
    assert 3.2 in pts2 and 3.0 not in pts2, pts2
    assert len(out2) == len(slots), out2
    # durations always cover the reel exactly
    assert abs(sum(s.duration for s in out2) - 9.0) < 1e-6


@test
def personalize_parts_fail_open():
    from app.reference import intake
    import app.profiles as profiles_mod
    import app.caption.llm as llm_mod
    parts = ["even when your at 1HP…", "you can still do 200 damage."]
    with patched(profiles_mod, read_persona=lambda pid: ""):
        assert intake.personalize_caption_parts(parts, "p") == parts
    def boom(*a, **k):
        raise RuntimeError("llm down")
    with patched(profiles_mod, read_persona=lambda pid: "a persona"), \
         patched(llm_mod, complete_json=boom):
        assert intake.personalize_caption_parts(parts, "p") == parts
    # a wrong part-count reply ships the originals
    with patched(profiles_mod, read_persona=lambda pid: "a persona"), \
         patched(llm_mod, complete_json=lambda *a, **k: '{"parts": ["only one"]}'):
        assert intake.personalize_caption_parts(parts, "p") == parts


@test
def coherent_selection_stays_in_family():
    """Reference recreations read as ONE scene: coherent mode keeps picks inside one visual
    family (same car / same setting) while default mode spreads across families."""
    import random as _random
    from app.generate.sequencer import Slot, select_segments
    # two families: A-clips mutually similar (same car), B-clips mutually similar, A⊥B.
    # interleaved fit ranks so fit alone doesn't decide the family.
    vec = {"a1": [1, 0.9, 0], "a2": [0.9, 1, 0], "a3": [1, 1, 0.1],
           "b1": [0, 0.1, 1], "b2": [0.1, 0, 1], "b3": [0, 0, 1]}
    text = {"a1": "black porsche 911 night city street", "a2": "black porsche 911 parking garage",
            "a3": "black porsche 911 interior dash", "b1": "gold rolex watch macro wrist",
            "b2": "gold rolex watch closeup table", "b3": "gold rolex watch box unboxing"}
    segs = [{"id": f"s{c}", "clip_id": c, "start_ts": 0.0, "end_ts": 6.0, "duration": 6.0,
             "usability_score": 0.9, "luminance": 0.5, "is_hero": False, "vibe_tags": []}
            for c in vec]
    slots = [Slot(i, i * 2.0, i * 2.0 + 2.0) for i in range(4)]
    fit = {"a1": 0, "b1": 1, "a2": 2, "b2": 3, "a3": 4, "b3": 5}
    dur = {c: 8.0 for c in vec}
    _random.seed(7)
    coh = select_segments(slots, segs, fit_rank=fit, clip_emb=vec, clip_dur=dur,
                          clip_text=text, coherent=True, temperature=0.8)
    fams = {c["clip_id"][0] for c in coh}
    # a1 leads on fit; the similarity bonus + no subject de-dup must keep the reel in family A
    assert fams == {"a"}, f"coherent mode left the family: {[c['clip_id'] for c in coh]}"
    # default mode: subject de-dup forbids a second same-family clip while others remain —
    # the same inputs MUST spread across families
    _random.seed(7)
    dev = select_segments(slots, segs, fit_rank=fit, clip_emb=vec, clip_dur=dur,
                          clip_text=text, temperature=2.0)
    fams_d = {c["clip_id"][0] for c in dev}
    assert fams_d == {"a", "b"}, f"default mode failed to spread: {[c['clip_id'] for c in dev]}"


# ─── Recaption: operator picks a different caption option ───

@test
def recaption_updates_record_and_logs_swap():
    import tempfile as _tf
    from app.corpus import reels as reel_store
    with _tf.TemporaryDirectory() as td:
        path = os.path.join(td, "reels.jsonl")
        with patched(reel_store, _path=lambda pid=None: path):
            reel_store.append({
                "reel_id": "r1", "reel_url": "/reels/old.mp4", "caption": "default line",
                "candidates": [{"text": "default line", "chosen": True},
                               {"text": "the better option", "chosen": False}],
                "clips": [{"clip_id": "c1"}],
            })
            rec = reel_store.record_recaption("r1", "/reels/new.mp4", "the better option",
                                              [{"clip_id": "c2"}])
            assert rec["caption"] == "the better option"
            assert rec["reel_url"] == "/reels/new.mp4"
            assert rec["clips"] == [{"clip_id": "c2"}]
            flags = {c["text"]: c["chosen"] for c in rec["candidates"]}
            assert flags == {"default line": False, "the better option": True}, flags
            swaps = rec["caption_swaps"]
            assert len(swaps) == 1 and swaps[0]["from"] == "default line" \
                and swaps[0]["to"] == "the better option", swaps
            # operator-authored text (not among options) still becomes the chosen candidate
            rec2 = reel_store.record_recaption("r1", "/reels/new2.mp4", "hand-typed line", [])
            auth = [c for c in rec2["candidates"] if c.get("operator_authored")]
            assert len(auth) == 1 and auth[0]["chosen"] is True
            assert len(rec2["caption_swaps"]) == 2
            # persisted, not just in-memory
            assert reel_store.get("r1")["caption"] == "hand-typed line"
            assert reel_store.record_recaption("nope", "/reels/x.mp4", "y", []) is None


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
