"""7.3 LLM provider layer (idea from TradingAgents' llm_clients; our own small version).

One function, `complete(system, user, deep=...)`, for every provider:

- anthropic          official `anthropic` SDK (pip install anthropic). Default model claude-opus-5-5,
                     refusal fallback on (server-side "default" routing) where the model supports it.
- openai             OpenAI chat-completions over HTTP.
- openai_compatible  any OpenAI-compatible server (vLLM, LM Studio, a relay) at INTEL_LLM_BASE_URL.
- ollama             a local model at http://localhost:11434/v1 (free, nothing leaves the PC).

Safety rails, all enforced here so callers can't forget them:
- off unless INTEL_LLM_PROVIDER is set: nothing is ever sent without it;
- every call is logged (tokens, cost, duration, errors) in LLMCall;
- a daily spend cap (USD) and a daily call cap block new calls once reached;
- identical prompts within INTEL_LLM_CACHE_HOURS are answered from the log for free.
Prompts only ever contain market data from this app: no personal or account data.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import timedelta

import requests
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import LLMCall

# USD per 1M tokens (input, output), first-party API list prices. Override with INTEL_LLM_PRICE_IN / _OUT.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}
# models with server-side refusal fallback ("default" routing)
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"}
DEFAULT_MODEL = {"anthropic": "claude-opus-5-5", "openai": "", "openai_compatible": "", "ollama": "llama3.1"}
DEFAULT_BASE = {"openai": "https://api.openai.com/v1", "ollama": "http://localhost:11434/v1"}
PROVIDERS = ("anthropic", "openai", "openai_compatible", "ollama")


class LLMError(Exception):
    pass


class NotConfigured(LLMError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


def cfg() -> dict:
    return settings.INTEL


def provider() -> str:
    p = cfg()["LLM_PROVIDER"]
    return p if p in PROVIDERS else ""


def model(deep: bool = True) -> str:
    c = cfg()
    deep_model = c["LLM_MODEL"] or DEFAULT_MODEL.get(provider(), "")
    return deep_model if deep else (c["LLM_QUICK_MODEL"] or deep_model)


def enabled() -> bool:
    return bool(provider()) and bool(model(deep=True))


def price(model_id: str) -> tuple[float, float] | None:
    c = cfg()
    if c["LLM_PRICE_IN"] and c["LLM_PRICE_OUT"]:
        return float(c["LLM_PRICE_IN"]), float(c["LLM_PRICE_OUT"])
    if provider() == "ollama":
        return 0.0, 0.0
    return PRICES.get(model_id)


def _today():
    return timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today() -> float:
    return LLMCall.objects.filter(created_at__gte=_today(), cached=False).aggregate(s=Sum("cost_usd"))["s"] or 0.0


def calls_today() -> int:
    return LLMCall.objects.filter(created_at__gte=_today(), cached=False).count()


def status() -> dict:
    c = cfg()
    m = model(True)
    return {
        "enabled": enabled(), "provider": provider() or "off", "model": m, "quick_model": model(False),
        "budget": c["LLM_DAILY_BUDGET_USD"], "spent": spent_today(), "calls": calls_today(),
        "max_calls": c["LLM_MAX_CALLS_PER_DAY"], "priced": price(m) is not None,
        "recent": list(LLMCall.objects.all()[:15]),
    }


def _key() -> str:
    c = cfg()
    if c["LLM_API_KEY"]:
        return c["LLM_API_KEY"]
    return os.environ.get("ANTHROPIC_API_KEY" if provider() == "anthropic" else "OPENAI_API_KEY", "")


# -- providers ----------------------------------------------------------------------------------------


def _anthropic(model_id: str, system: str, user: str, max_tokens: int, effort: str) -> Reply:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise LLMError("The anthropic package is not installed: pip install anthropic") from exc
    client = anthropic.Anthropic(api_key=_key() or None)  # no key: the SDK's own credential lookup
    kwargs = dict(model=model_id, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}], output_config={"effort": effort})
    try:
        if model_id in FALLBACK_MODELS:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.APIStatusError as exc:
        raise LLMError(f"Anthropic API {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Anthropic API unreachable") from exc
    if resp.stop_reason == "refusal":
        raise LLMError("The model declined this request")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return Reply(text, resp.usage.input_tokens or 0, resp.usage.output_tokens or 0)


def _openai_like(model_id: str, system: str, user: str, max_tokens: int, effort: str) -> Reply:
    base = (cfg()["LLM_BASE_URL"] or DEFAULT_BASE.get(provider(), "")).rstrip("/")
    if not base:
        raise NotConfigured("Set INTEL_LLM_BASE_URL for an OpenAI-compatible provider")
    headers = {"Content-Type": "application/json"}
    if _key():
        headers["Authorization"] = f"Bearer {_key()}"
    body = {"model": model_id, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    last = LLMError(f"{provider()} failed")
    for attempt in range(3):
        try:
            r = requests.post(f"{base}/chat/completions", json=body, headers=headers, timeout=300)
        except requests.RequestException as exc:
            last = LLMError(f"{provider()} unreachable ({exc.__class__.__name__})")
        else:
            if r.ok:
                d = r.json()
                u = d.get("usage") or {}
                return Reply(d["choices"][0]["message"].get("content") or "", u.get("prompt_tokens", 0),
                             u.get("completion_tokens", 0))
            last = LLMError(f"{provider()} HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code not in (408, 409, 429) and r.status_code < 500:
                break
        time.sleep(2 ** attempt)
    raise last


def _call_provider(model_id: str, system: str, user: str, max_tokens: int, effort: str) -> Reply:
    if provider() == "anthropic":
        return _anthropic(model_id, system, user, max_tokens, effort)
    return _openai_like(model_id, system, user, max_tokens, effort)


# -- the one entry point --------------------------------------------------------------------------------


def complete(system: str, user: str, *, deep: bool = False, purpose: str = "", max_tokens: int = 16000,
             effort: str | None = None) -> str:
    """Run one prompt through the configured provider, within the budget, logged. Returns the text."""
    if not enabled():
        raise NotConfigured("No LLM provider configured (set INTEL_LLM_PROVIDER)")
    c = cfg()
    model_id = model(deep)
    effort = effort or ("medium" if deep else "low")
    key = hashlib.sha256(f"{provider()}|{model_id}|{effort}|{system}|{user}".encode()).hexdigest()
    hit = LLMCall.objects.filter(cache_key=key, ok=True, cached=False,
                                 created_at__gte=timezone.now() - timedelta(hours=c["LLM_CACHE_HOURS"])).first()
    if hit:
        LLMCall.objects.create(provider=provider(), model=model_id, purpose=purpose[:100], cached=True, cache_key=key)
        return hit.response
    if spent_today() >= c["LLM_DAILY_BUDGET_USD"]:
        raise BudgetExceeded(f"Daily AI budget of ${c['LLM_DAILY_BUDGET_USD']:.2f} reached")
    if calls_today() >= c["LLM_MAX_CALLS_PER_DAY"]:
        raise BudgetExceeded(f"Daily limit of {c['LLM_MAX_CALLS_PER_DAY']} AI calls reached")
    started = time.monotonic()
    try:
        reply = _call_provider(model_id, system, user, max_tokens, effort)
    except LLMError as exc:
        LLMCall.objects.create(provider=provider(), model=model_id, purpose=purpose[:100], ok=False, error=str(exc)[:2000],
                               duration_ms=int((time.monotonic() - started) * 1000))
        raise
    p = price(model_id) or (0.0, 0.0)
    cost = reply.input_tokens / 1e6 * p[0] + reply.output_tokens / 1e6 * p[1]
    LLMCall.objects.create(provider=provider(), model=model_id, purpose=purpose[:100], input_tokens=reply.input_tokens,
                           output_tokens=reply.output_tokens, cost_usd=cost, cache_key=key, response=reply.text,
                           duration_ms=int((time.monotonic() - started) * 1000))
    return reply.text
