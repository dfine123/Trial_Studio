"""Provider-agnostic LLM completion for the caption engine — Claude or OpenAI, by config.

Lets the identical pipeline (corpus, grades, prompts, refine) run on either model for a fair
A/B. Returns the model's raw text; callers do their own JSON parsing. Selected per process via
settings.caption_provider (env CAPTION_PROVIDER).
"""
from __future__ import annotations

from app.config import settings


def complete_json(system: str, user: str, effort: str = "high", max_tokens: int = 4000) -> str:
    if settings.caption_provider == "openai":
        return _openai(system, user, max_tokens)
    return _anthropic(system, user, effort, max_tokens)


def _anthropic(system: str, user: str, effort: str, max_tokens: int) -> str:
    from anthropic import Anthropic

    msg = Anthropic(api_key=settings.anthropic_api_key, max_retries=5).messages.create(
        model=settings.caption_model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def _openai(system: str, user: str, max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key, max_retries=4)
    resp = client.chat.completions.create(
        model=settings.openai_caption_model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=1.0,
    )
    return resp.choices[0].message.content or ""
