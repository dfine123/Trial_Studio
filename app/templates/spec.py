"""Pydantic schema for a Template's `spec` JSON — the dual record (Recipe + Formula Object).

Deliberately FREE-FORM. Templates are authored case-by-case in the studio: variable segment count,
variable structure, captions optional, NO fixed taxonomy (no forced setup/pivot/payoff, no fixed
relatable/transitional/aspirational tiers). The author describes each segment's clip-type and
example caption in their own words; the LLM reads THIS template and infers its own formula.

Recipe (Half A) = ordered beat-snapped time-segments + the author's per-segment clip-type + caption
exemplars. Formula Object (Half B) = the LLM's interpretation of what this specific template does +
how to re-skin it onto a new creator. Boundaries are TIMES in seconds (beat-snapped at authoring).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ClipCriteria(BaseModel):
    """Free-form description of the clip that fills this segment — the author's words, e.g.
    'older candid', 'the flex', 'gym mirror', 'outfit 1', 'the reveal'. The LLM matches a creator's
    clips to this description from their existing indexing. NOT a fixed tier enum."""
    clip_type: str = ""                # free-form
    motion_pref: Literal["still", "gentle", "dynamic", "any"] = "any"


class SegmentSpec(BaseModel):
    index: int
    t_in: float                        # seconds on the audio timeline (beat-snapped at authoring)
    t_out: float
    source_type: Literal["creator_clip", "fixed_asset", "generated"] = "creator_clip"
    asset_id: str | None = None        # bound verbatim when fixed_asset
    clip_criteria: ClipCriteria | None = None
    caption_slot_id: str | None = None  # a segment MAY have no caption
    transition: Literal["cut", "fade"] = "cut"


class CaptionSlot(BaseModel):
    id: str
    segment_index: int
    role: str | None = None            # FREE-FORM author note (e.g. "the hook", "the turn") — optional, NOT an enum
    exemplar: str | None = None        # the author's example caption (a pattern, regenerated per creator)
    y_frac: float | None = None


class SlotVariability(BaseModel):
    """Per-caption-slot model of HOW MUCH this caption can change when re-skinned — inferred by the
    LLM from the author's hints, NOT a fixed level. Tight slots vary only when conditions warrant
    ('if the stars align'); loose slots are fill-in-the-blanks rewritten per creator."""
    slot_id: str
    locked_structure: str = ""          # what MUST stay the same
    variables: list[str] = Field(default_factory=list)   # the swappable parts (e.g. the keyword, the (insert))
    vary_when: str = ""                 # the CONDITION under which to actually vary
    flexibility: str = "medium"         # low | medium | high — quick signal for the regenerator


class FormulaObject(BaseModel):
    """The LLM's free-form interpretation of THIS specific template. No assumed structure."""
    title: str = ""
    formula: str = ""                  # NL: what this template DOES and why it works
    caption_logic: str = ""            # NL: how the captions relate across segments — if they do at all
    exemplar_arc: list[str] = Field(default_factory=list)   # the authored captions in order (a pattern, never copied)
    reskin_rules: str = ""             # how to regenerate for a new creator: invariant vs swappable
    slots: list[SlotVariability] = Field(default_factory=list)   # per-slot variability model (the intelligence)
    marks_hash: str = ""               # sha1 of the recipe marks this formula was enriched from (drift guard)


class TemplateSpec(BaseModel):
    segments: list[SegmentSpec]
    caption_slots: list[CaptionSlot] = Field(default_factory=list)
    formula: FormulaObject = Field(default_factory=FormulaObject)
    min_seg_len: float = 1.0           # below this a segment is too short to read; refuse

    @model_validator(mode="after")
    def _check(self) -> "TemplateSpec":
        slot_ids = {c.id for c in self.caption_slots}
        for seg in self.segments:
            if seg.caption_slot_id and seg.caption_slot_id not in slot_ids:
                raise ValueError(f"segment {seg.index} references missing caption_slot {seg.caption_slot_id!r}")
            if seg.t_out - seg.t_in < self.min_seg_len:
                raise ValueError(f"segment {seg.index} ({seg.t_out - seg.t_in:.2f}s) is below min_seg_len {self.min_seg_len}s")
        return self
