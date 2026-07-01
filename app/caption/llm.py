"""Provider-agnostic LLM completion for the caption engine — Claude or OpenAI, by config.

Lets the identical pipeline (corpus, grades, prompts, refine) run on either model for a fair
A/B. Returns the model's raw text; callers do their own JSON parsing. Selected per process via
settings.caption_provider (env CAPTION_PROVIDER).
"""
from __future__ import annotations

from app.config import settings


def complete_json(system: str, user: str, effort: str = "high", max_tokens: int = 4000) -> str:
    # A per-request TEST backend (Sonnet 5 / OpenAI) overrides the model; None → production (settings).
    from app.caption.backend import get_backend, resolve
    override = resolve(get_backend())
    if override:
        provider, model = override
        if provider == "openai":
            return _openai(system, user, max_tokens, model=model, effort=effort)
        return _anthropic(system, user, effort, max_tokens, model=model)
    if settings.caption_provider == "openai":
        return _openai(system, user, max_tokens)
    return _anthropic(system, user, effort, max_tokens)


def _anthropic(system: str, user: str, effort: str, max_tokens: int, model: str | None = None) -> str:
    from anthropic import Anthropic

    msg = Anthropic(api_key=settings.anthropic_api_key, max_retries=5).messages.create(
        model=model or settings.caption_model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def _openai(system: str, user: str, max_tokens: int, model: str | None = None, effort: str = "high") -> str:
    from openai import OpenAI

    m = model or settings.openai_caption_model
    client = OpenAI(api_key=settings.openai_api_key, max_retries=4)
    kwargs: dict = dict(
        model=m,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        # gpt-5.x: use max_completion_tokens (not max_tokens), leave headroom for internal
        # reasoning, and omit temperature (reasoning models reject a custom value).
        max_completion_tokens=max(max_tokens, 8000),
    )
    if m.startswith(("gpt-5", "o3", "o4")):     # reasoning models take reasoning_effort (mirror our effort)
        kwargs["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "medium"
    return client.chat.completions.create(**kwargs).choices[0].message.content or ""
