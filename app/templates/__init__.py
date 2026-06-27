"""Template Studio — beat-segmented progression templates (CapCut-authored, LLM-interpreted).

A template is a DUAL RECORD (see `spec.TemplateSpec`):
  - Recipe (Half A): ordered beat-snapped time-segments + per-segment clip criteria + caption-slot
    roles. The matcher + compositor execute this deterministically.
  - Formula Object (Half B): the LLM-authored, editable why/how (progression logic, the
    one-sentence-across-cuts caption grammar, re-skin rules) the LLM reasons over to regenerate a
    different-but-formula-faithful caption arc per creator.
"""
