"""Provider-agnostic LLM completion for the caption engine — Claude or OpenAI, by config.

Lets the identical pipeline (corpus, grades, prompts, refine) run on either model for a fair
A/B. Returns the model's raw text; callers do their own JSON parsing. Selected per process via
settings.caption_provider (env CAPTION_PROVIDER).
"""
from __future__ import annotations

from app.config import settings


def complete_json(system: str, user: str, effort: str = "high", max_tokens: int = 4000,
                  cache_system: bool = False, tag: str = "-",
                  cache_user_prefix: str | None = None) -> str:
    # A per-request TEST backend (Sonnet 5 / OpenAI) overrides the model; None → production (settings).
    # cache_system marks the SYSTEM block as an ephemeral cache prefix (Anthropic prompt caching):
    # byte-identical reuse within the 5-min TTL bills at ~10% — a pure billing/latency optimization,
    # generation behavior is untouched. Set it ONLY where reuse is real (the k parallel candidate
    # calls share one system; chooser/refine systems are stable across a sequential batch) — a
    # marked-but-never-reused system >1024 tokens costs a 1.25x write surcharge for nothing.
    from app.caption.backend import get_backend, resolve
    override = resolve(get_backend())
    if override:
        provider, model = override
        if provider == "openai":
            return _openai(system, user, max_tokens, model=model, effort=effort)
        return _anthropic(system, user, effort, max_tokens, model=model, cache_system=cache_system,
                          tag=tag, cache_user_prefix=cache_user_prefix)
    if settings.caption_provider == "openai":
        return _openai(system, user, max_tokens)
    return _anthropic(system, user, effort, max_tokens, cache_system=cache_system, tag=tag,
                      cache_user_prefix=cache_user_prefix)


def _anthropic(system: str, user: str, effort: str, max_tokens: int, model: str | None = None,
               cache_system: bool = False, tag: str = "-", cache_user_prefix: str | None = None) -> str:
    from anthropic import Anthropic

    sys_payload = ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                   if cache_system else system)
    # cache_user_prefix: a large, call-to-call-stable LEADING chunk of the user message (e.g. the
    # clip listing the matcher ranks) cached as its own prefix block; the varying part follows it.
    # Everything before the breakpoint (system + prefix) must be byte-identical to hit.
    if cache_user_prefix:
        content = [{"type": "text", "text": cache_user_prefix, "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": user}]
    else:
        content = user
    msg = Anthropic(api_key=settings.anthropic_api_key, max_retries=5).messages.create(
        model=model or settings.caption_model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=sys_payload,
        messages=[{"role": "user", "content": content}],
    )
    u = msg.usage
    print(f"[llm] tag={tag} {model or settings.caption_model} eff={effort} in={u.input_tokens} "
          f"out={u.output_tokens} cache_w={getattr(u, 'cache_creation_input_tokens', 0) or 0} "
          f"cache_r={getattr(u, 'cache_read_input_tokens', 0) or 0}", flush=True)
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
