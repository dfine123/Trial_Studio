"""Per-REQUEST model backend — isolated A/B testing (Sonnet 5 / OpenAI) alongside production Opus.

A ContextVar picks the backend for the current request/task. `complete_json` routes the model by it, and
the per-profile MUTABLE-state paths (reels, taste, ref scores/usage, generated log) take a per-backend
SUFFIX — so a test backend reads the SAME voice (references + persona) but reads/writes its OWN isolated
output/grade/rotation state. Default (None) = production Opus, entirely unchanged. Temporary testing rig.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

_BACKEND: contextvars.ContextVar[str | None] = contextvars.ContextVar("caption_backend", default=None)

# name -> (provider, model). Only the TEST backends live here; None = production (settings-driven Opus).
# gpt-5.5 is OpenAI's flagship; gpt-5.5-pro exists but is impractically slow for a multi-call pipeline.
BACKENDS: dict[str, tuple[str, str]] = {
    "sonnet": ("anthropic", "claude-sonnet-5"),
    "openai": ("openai", "gpt-5.5"),
}


def valid(name: str | None) -> str | None:
    """Normalize an incoming backend name to a known test backend, else None (production)."""
    return name if name in BACKENDS else None


def get_backend() -> str | None:
    return _BACKEND.get()


def resolve(name: str | None) -> tuple[str, str] | None:
    """(provider, model) for the active/given backend, or None → production (use settings)."""
    return BACKENDS.get(name) if name else None


def suffix() -> str:
    """Filename suffix isolating a test backend's MUTABLE state ('' for production)."""
    b = _BACKEND.get()
    return f"__{b}" if b in BACKENDS else ""


@contextmanager
def using(name: str | None):
    """Run a block under a test backend (model routing + isolated state). Unknown/None → production."""
    token = _BACKEND.set(valid(name))
    try:
        yield
    finally:
        _BACKEND.reset(token)
