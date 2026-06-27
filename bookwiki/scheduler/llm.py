from __future__ import annotations

import asyncio
import base64
import contextvars
import inspect
import json
import mimetypes
import os
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from bookwiki.scheduler.budget_guard import BudgetExceeded
from bookwiki.utils.logging import get_logger

_LOG = get_logger(__name__)

# A tool executor maps ``(tool_name, arguments)`` to a JSON-serialisable result.
# It may be sync or async; ``generate_with_tools`` awaits awaitable results.
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


# litellm leaves requests on the default (very long) socket timeout; a single hung
# upstream request would otherwise stall a chapter indefinitely. Cap per-request
# wall time so a stuck call surfaces as an error the backoff/repair paths can see.
LLM_REQUEST_TIMEOUT_SECONDS = 600
DEFAULT_MOONSHOT_API_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter bills in USD; BookWiki budget accounting is CNY, so registered
# prices are converted with a fixed guardrail rate. This is deliberately
# conservative enough for local budget enforcement, not invoice reconciliation.
OPENROUTER_USD_TO_CNY = 6.8

# Per-token prices (CNY) registered on each Router deployment so litellm computes
# ``response_cost`` (read back by ``_record_usage``) and the ``maxCostCny`` budget is
# actually enforceable. litellm has no built-in pricing for these custom model names,
# so without this every call costs 0.
#
# Currency: CNY (¥). The configured endpoints (api.moonshot.cn, DeepSeek domestic) bill
# in RMB, so we price in RMB to match the real invoice with no FX drift.
#
# ─────────────────────────────────────────────────────────────────────────────
# ADDING / CHANGING A MODEL — update THESE places (all in this file unless noted):
#   1. _model_list()        — register the Router deployment: model_name → provider
#                             path, api_key, timeout, tpm/rpm.
#   2. _MODEL_PRICES_CNY     — per-1M-token price (input / output / cache-hit). Skip
#                             it and the call is billed as 0 — litellm has no built-in
#                             price for our custom model names.
#   3. _MODEL_CONTEXT_WINDOW — (context_window, max_output) for the input token
#                             budget. litellm's own table does NOT know our custom
#                             names (e.g. deepseek-v4-*), so a model missing here
#                             silently falls back to _UNKNOWN_INPUT_TOKEN_BUDGET.
#   4. _api_key_env()        — only when a NEW provider/prefix is introduced.
#   (config.py DEFAULT_MODELS — only to change which agent uses which model.)
# ─────────────────────────────────────────────────────────────────────────────
# Cached vs uncached input: each tuple is (input_miss, output, input_cache_hit) per 1M
# tokens. Context caching makes cache hits far cheaper on input, so we register the hit
# rate separately (litellm prices ``cached_tokens`` at it). Source — official platform
# pricing (元/百万 tokens): deepseek-v4-flash ¥1/¥2, hit ¥0.02; deepseek-v4-pro ¥3/¥6,
# hit ¥0.025; kimi-k2.6 ¥6.5/¥27, hit ¥1.10.
_MILLION = 1_000_000.0
_MODEL_PRICES_CNY: dict[str, tuple[float, float, float]] = {
    "deepseek-v4-flash": (1.0 / _MILLION, 2.0 / _MILLION, 0.02 / _MILLION),
    "deepseek-v4-pro": (3.0 / _MILLION, 6.0 / _MILLION, 0.025 / _MILLION),
    "kimi-k2.6": (6.5 / _MILLION, 27.0 / _MILLION, 1.10 / _MILLION),
    # OpenRouter reported pricing for qwen/qwen3.6-35b-a3b at test time:
    # $0.20 / $1.60 per 1M input/output tokens. No cache discount is assumed.
    "openrouter-qwen3.6-35b-a3b": (
        0.20 * OPENROUTER_USD_TO_CNY / _MILLION,
        1.60 * OPENROUTER_USD_TO_CNY / _MILLION,
        0.20 * OPENROUTER_USD_TO_CNY / _MILLION,
    ),
}


def _price_params(model_name: str) -> dict[str, float]:
    """litellm_params cost keys for a deployment, or empty if unpriced."""
    price = _MODEL_PRICES_CNY.get(model_name)
    if price is None:
        return {}
    input_cost, output_cost, cache_read_cost = price
    return {
        "input_cost_per_token": input_cost,
        "output_cost_per_token": output_cost,
        "cache_read_input_token_cost": cache_read_cost,
    }


# --- Input token budgeting -------------------------------------------------
# Context windows (total tokens, shared input+output) and max output per the
# official model cards (2026-04): deepseek-v4-pro/flash 1,048,576 ctx / 384,000
# out; kimi-k2.6 262,144 ctx / 98,304 out. Reserving the model's full max output
# as headroom guarantees a single oversized input field can never crowd out the
# response or overflow the window.
_MODEL_CONTEXT_WINDOW: dict[str, tuple[int, int]] = {
    "deepseek-v4-flash": (1_048_576, 384_000),
    "deepseek-v4-pro": (1_048_576, 384_000),
    "kimi-k2.6": (262_144, 98_304),
    "openrouter-qwen3.6-35b-a3b": (262_144, 65_536),
}
_INPUT_BUDGET_SAFETY = 2_048
# Unknown model: generous guard above the largest field seen in real runs
# (~130k tokens) plus headroom, still bounded so a pathological field is capped.
_UNKNOWN_INPUT_TOKEN_BUDGET = 200_000
# When the ``runtime`` extra (litellm/tiktoken) is absent we cannot tokenize, so
# we estimate token count from characters. cl100k on mixed Chinese/LaTeX/English
# MDX runs roughly this many characters per token; budgets are several times
# larger than real inputs, so the estimate only needs to be order-of-magnitude.
_FALLBACK_CHARS_PER_TOKEN = 3


def input_token_budget(model: str) -> int:
    """Max tokens for a single input field before ``compact_input`` truncates it.

    Equals ``context_window - max_output - safety`` so an oversized field can
    never crowd out the model's response or overflow the window. Unknown models
    fall back to a fixed generous guard.
    """
    entry = _MODEL_CONTEXT_WINDOW.get(model)
    if entry is None:
        return _UNKNOWN_INPUT_TOKEN_BUDGET
    context_window, max_output = entry
    budget = context_window - max_output - _INPUT_BUDGET_SAFETY
    return budget if budget > 0 else context_window - _INPUT_BUDGET_SAFETY


def count_text_tokens(text: str, *, model: str) -> int:
    """Token count for ``text`` under ``model``.

    Uses litellm's tokenizer when the ``runtime`` extra is installed (unknown
    custom model names fall back to cl100k_base inside litellm — an approximation
    that is fine for budgeting). Without litellm, estimates from character length.
    """
    try:
        from litellm import token_counter
    except Exception:
        return max(1, len(text) // _FALLBACK_CHARS_PER_TOKEN)
    try:
        return int(token_counter(model=model, text=text))
    except Exception:
        return max(1, len(text) // _FALLBACK_CHARS_PER_TOKEN)


_RATE_LIMIT_MARKERS = (
    "ratelimit",
    "rate limit",
    "no deployments available",
    "routerratelimiterror",
    "too many requests",
    "429",
)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """True if ``exc`` (or anything in its cause/context chain) is a rate-limit error.

    Rate limits surface deeply nested: instructor wraps in ``InstructorRetryException``,
    the router in ``RouterRateLimitError``, tenacity in ``RetryError``. We walk the chain
    and match on type name / message rather than importing optional litellm types.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__.lower()
        message = str(current).lower()
        if "ratelimit" in name or any(marker in message for marker in _RATE_LIMIT_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _acreate_with_backoff(
    factory: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 6,
    base_delay: float = 5.0,
    max_delay: float = 90.0,
) -> Any:
    """Call ``factory()``, retrying rate-limit errors with exponential backoff.

    A sustained rate limit puts the deployment into cooldown (litellm default ~60s),
    far longer than the in-call retries (router ``num_retries`` + instructor
    ``max_retries``), so a busy run would otherwise crash. Here we wait the limit out:
    delays grow ~5s, 10s, 20s, 40s, 80s (capped at ``max_delay``, jittered) — a budget
    that outlasts the cooldown. Non-rate-limit errors propagate immediately.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await factory()
        except Exception as exc:
            if not _is_rate_limit_error(exc) or attempt == max_attempts - 1:
                raise
            last_exc = exc
            delay = min(max_delay, base_delay * (2.0**attempt)) + random.uniform(0.0, base_delay)
            _LOG.warning(
                "LLM rate limited (attempt %d/%d); backing off %.1fs: %s",
                attempt + 1,
                max_attempts,
                delay,
                str(exc)[:160],
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


class LLMRuntime(Protocol):
    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
        max_tokens: int | None = None,
    ) -> BaseModel:
        """Return a validated structured result from a real or explicitly injected LLM."""

    async def generate_with_tools(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        tools: Sequence[dict[str, Any]],
        tool_executor: ToolExecutor,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_tool_rounds: int = 4,
        max_retries: int = 2,
    ) -> BaseModel:
        """Run an OpenAI-style tool-calling loop, then coerce a structured result."""

    async def generate_document(
        self,
        *,
        model: str,
        system: str,
        user: str,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
    ) -> str:
        """Return raw document text from a real or explicitly injected LLM."""


class MissingLLMApiKey(RuntimeError):
    def __init__(self, model: str, env_name: str) -> None:
        super().__init__(f"model {model!r} requires ${env_name}; configure it before running")
        self.model = model
        self.env_name = env_name


class UnsupportedLLMModel(RuntimeError):
    def __init__(self, model: str) -> None:
        super().__init__(f"unsupported LLM model {model!r}; configure a DeepSeek or Kimi model")
        self.model = model


class LLMRuntimeUnavailable(RuntimeError):
    """Raised when the real runtime dependencies are not installed."""


class ToolLoopExceeded(RuntimeError):
    """Raised when a tool-calling loop never converges to a final answer."""

    def __init__(self, model: str, max_tool_rounds: int) -> None:
        super().__init__(
            f"model {model!r} kept calling tools after {max_tool_rounds} rounds; aborting"
        )
        self.model = model
        self.max_tool_rounds = max_tool_rounds


# Per-stage usage attribution. ``_record_usage`` runs in whatever asyncio Task
# issued the API call, and LangGraph executes each node — including the concurrent
# ``Send`` fanout siblings ``generate_chapter`` / ``concept_page`` — in its own
# Task with an isolated copy of the context. Binding a fresh accumulator at node
# start therefore captures exactly the calls that node makes, even when siblings
# interleave at ``await`` points. Diffing the shared global counters instead
# over-counts: overlapping before/after windows make every sibling record a
# cumulative running total, and summing those snapshots inflates the manifest's
# ``total_cost_cny`` (the regression this attribution fixes).
_ACTIVE_STAGE_USAGE: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "bookwiki_active_stage_usage", default=None
)


def begin_stage_usage() -> dict[str, float]:
    """Bind a fresh per-task usage accumulator for the current stage and return it.

    The returned dict is mutated in place by every :func:`record_stage_usage` call
    made within the same context, so the caller reads the stage's own usage back
    once it finishes.
    """
    accumulator: dict[str, float] = {
        "cost_cny": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    _ACTIVE_STAGE_USAGE.set(accumulator)
    return accumulator


def record_stage_usage(*, cost_cny: float, prompt_tokens: int, completion_tokens: int) -> None:
    """Attribute one API call's usage to the stage active in the current context."""
    accumulator = _ACTIVE_STAGE_USAGE.get()
    if accumulator is None:
        return
    accumulator["cost_cny"] += cost_cny
    accumulator["prompt_tokens"] += prompt_tokens
    accumulator["completion_tokens"] += completion_tokens


class LiteLLMRuntime:
    def __init__(self, router: Any | None = None, *, max_cost_cny: float | None = None) -> None:
        self.router = router
        self.client: Any | None = None
        # Lazy router/client construction is shared across concurrently generating
        # chapters; without this lock the first few parallel calls would each build
        # their own Router (defeating its tpm/rpm self-throttling). Double-checked
        # under the lock so the steady-state path stays lock-free.
        self._init_lock = asyncio.Lock()
        # Usage/cost accounting accumulates across every API call on this runtime.
        # ``max_cost_cny`` (<= 0 or None means unlimited) bounds the whole run.
        self._max_cost_cny = max_cost_cny
        self.total_cost_cny = 0.0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def _record_usage(self, response: Any, *, model: str | None = None) -> None:
        """Accumulate token/cost usage from one API response and enforce the budget.

        Hooked into every real API call (instructor JSON path via the
        ``build_instructor_client`` wrapper; document/tool paths directly). Robust to
        dict or object responses and to missing usage/cost fields (records 0). Raises
        :class:`BudgetExceeded` once the accumulated cost crosses ``max_cost_cny``.
        """
        prompt_tokens, completion_tokens = _extract_usage(response)
        cost = _extract_cost(response)
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_cost_cny += cost
        record_stage_usage(
            cost_cny=cost, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        _LOG.info(
            "llm usage model=%s prompt_tokens=%d completion_tokens=%d "
            "cost_cny=%.6f total_cost_cny=%.6f",
            model or _get_attr_or_key(response, "model") or "unknown",
            prompt_tokens,
            completion_tokens,
            cost,
            self.total_cost_cny,
        )
        if (
            self._max_cost_cny is not None
            and self._max_cost_cny > 0
            and self.total_cost_cny > self._max_cost_cny
        ):
            raise BudgetExceeded(
                f"budget exceeded: spent ¥{self.total_cost_cny:.4f}, "
                f"limit ¥{self._max_cost_cny:.4f}"
            )

    async def _ensure_router(self) -> Any:
        if self.router is not None:
            return self.router
        async with self._init_lock:
            if self.router is None:
                self.router = build_router()
            return self.router

    async def _ensure_client(self) -> Any:
        router = await self._ensure_router()
        if self.client is not None:
            return self.client
        async with self._init_lock:
            if self.client is None:
                self.client = build_instructor_client(router, on_usage=self._record_usage)
            return self.client

    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
        max_tokens: int | None = None,
    ) -> BaseModel:
        _ensure_api_key(model)
        client = await self._ensure_client()
        result = await _acreate_with_backoff(
            lambda: client.create(
                model=model,
                response_model=output_model,
                messages=_messages(system=system, user=user, image_paths=image_paths),
                max_retries=max_retries,
                **_completion_params_for_model(model, max_tokens=max_tokens),
            )
        )
        if isinstance(result, output_model):
            return output_model.model_validate(result.model_dump(mode="json"), context=context)
        return output_model.model_validate(result, context=context)

    async def generate_document(
        self,
        *,
        model: str,
        system: str,
        user: str,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
    ) -> str:
        _ensure_api_key(model)
        router = await self._ensure_router()
        response = await _acreate_with_backoff(
            lambda: router.acompletion(
                model=model,
                messages=_messages(system=system, user=user, image_paths=image_paths),
                max_retries=max_retries,
                **_completion_params_for_model(model),
            )
        )
        self._record_usage(response, model=model)
        content = response.choices[0].message.content
        if content is None:
            msg = f"model {model!r} returned an empty document response"
            raise ValueError(msg)
        return _strip_mdx_fence(str(content))

    async def generate_with_tools(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        tools: Sequence[dict[str, Any]],
        tool_executor: ToolExecutor,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_tool_rounds: int = 4,
        max_retries: int = 2,
    ) -> BaseModel:  # pragma: no cover - exercised only against a real LiteLLM router
        _ensure_api_key(model)
        router = await self._ensure_router()
        messages = _messages(system=system, user=user, image_paths=image_paths)
        for _round in range(max_tool_rounds):
            response = await _acreate_with_backoff(
                lambda: router.acompletion(
                    model=model,
                    messages=messages,
                    tools=list(tools),
                    tool_choice="auto",
                    **_completion_params_for_model(model),
                )
            )
            self._record_usage(response, model=model)
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                _LOG.info("tool loop done model=%s rounds_used=%d", model, _round)
                break
            _LOG.info(
                "tool round model=%s round=%d/%d calls=%d",
                model,
                _round + 1,
                max_tool_rounds,
                len(tool_calls),
            )
            messages.append(_assistant_tool_message(message))
            for call in tool_calls:
                messages.append(await _run_tool_call(call, tool_executor))
        else:
            raise ToolLoopExceeded(model, max_tool_rounds)

        client = await self._ensure_client()
        result = await _acreate_with_backoff(
            lambda: client.create(
                model=model,
                response_model=output_model,
                messages=[
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Return the final answer as JSON matching the requested schema. "
                            "Do not call tools."
                        ),
                    },
                ],
                max_retries=max_retries,
                **_completion_params_for_model(model),
            )
        )
        if isinstance(result, output_model):
            return output_model.model_validate(result.model_dump(mode="json"), context=context)
        return output_model.model_validate(result, context=context)


class TestLLMRuntime:
    """Explicit fake runtime for subprocess smoke tests."""

    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
        max_tokens: int | None = None,
    ) -> BaseModel:
        draft = _extract_draft_payload(user)
        if draft is None:
            msg = f"test runtime needs a Draft JSON block for {output_model.__name__}"
            raise ValueError(msg)
        return output_model.model_validate(draft, context=context)

    async def generate_document(
        self,
        *,
        model: str,
        system: str,
        user: str,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
    ) -> str:
        draft = _extract_draft_document(user)
        if draft is None:
            msg = f"test runtime needs a Draft Document block for {model}"
            raise ValueError(msg)
        return draft

    async def generate_with_tools(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        tools: Sequence[dict[str, Any]],
        tool_executor: ToolExecutor,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_tool_rounds: int = 4,
        max_retries: int = 2,
    ) -> BaseModel:
        # Deterministic offline path: echo the Draft JSON, never invoke tools.
        draft = _extract_draft_payload(user)
        if draft is None:
            msg = f"test runtime needs a Draft JSON block for {output_model.__name__}"
            raise ValueError(msg)
        return output_model.model_validate(draft, context=context)


def build_runtime(*, max_cost_cny: float | None = None) -> LLMRuntime:
    load_dotenv()
    if os.getenv("BOOKWIKI_TEST_LLM") == "1":
        return TestLLMRuntime()
    return LiteLLMRuntime(max_cost_cny=max_cost_cny)


def build_router() -> Any:
    load_dotenv()
    try:
        from litellm import Router
    except Exception as exc:  # pragma: no cover - exercised only without optional extra
        raise LLMRuntimeUnavailable(
            "LiteLLM is required for real LLM calls; install the runtime extra"
        ) from exc

    # No cross-model fallback: pro/flash are deliberately chosen per task (quality vs
    # cost) and are NOT interchangeable. Rate limits are handled by waiting the cooldown
    # out on the SAME model (see ``_acreate_with_backoff``), so a task always runs on its
    # intended model instead of silently degrading to / inflating onto the other.
    return Router(
        model_list=_model_list(),
        routing_strategy="usage-based-routing-v2",
        num_retries=3,
        retry_after=2,
    )


def _model_list() -> list[dict[str, Any]]:
    load_dotenv()
    return [
        {
            "model_name": "deepseek-v4-pro",
            "litellm_params": {
                "model": "deepseek/deepseek-v4-pro",
                "api_key": os.getenv("DEEPSEEK_API_KEY"),
                **_api_base_params("DEEPSEEK"),
                "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
                **_price_params("deepseek-v4-pro"),
            },
            "tpm": 200_000,
            "rpm": 60,
        },
        {
            "model_name": "deepseek-v4-flash",
            "litellm_params": {
                "model": "deepseek/deepseek-v4-flash",
                "api_key": os.getenv("DEEPSEEK_API_KEY"),
                **_api_base_params("DEEPSEEK"),
                "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
                **_price_params("deepseek-v4-flash"),
            },
            "tpm": 400_000,
            "rpm": 120,
        },
        {
            "model_name": "kimi-k2.6",
            "litellm_params": {
                "model": "moonshot/kimi-k2.6",
                "api_key": os.getenv("MOONSHOT_API_KEY"),
                **_api_base_params("MOONSHOT", default=DEFAULT_MOONSHOT_API_BASE_URL),
                "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
                **_price_params("kimi-k2.6"),
            },
        },
        {
            "model_name": "openrouter-qwen3.6-35b-a3b",
            "litellm_params": {
                "model": "openai/qwen/qwen3.6-35b-a3b",
                "api_key": _provider_api_key("OPENROUTER"),
                **_api_base_params("OPENROUTER", default=DEFAULT_OPENROUTER_API_BASE_URL),
                "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
                **_price_params("openrouter-qwen3.6-35b-a3b"),
            },
        },
    ]


def _api_base_params(
    provider: str,
    *,
    default: str | None = None,
    extra_env_names: tuple[str, ...] = (),
) -> dict[str, str]:
    api_base = _provider_api_base(provider, default=default, extra_env_names=extra_env_names)
    return {"api_base": api_base} if api_base else {}


def _provider_api_base(
    provider: str,
    *,
    default: str | None = None,
    extra_env_names: tuple[str, ...] = (),
) -> str | None:
    for env_name in (
        f"{provider}_API_BASE_URL",
        f"{provider}_API_BASE",
        *extra_env_names,
    ):
        value = os.getenv(env_name)
        if value and value.strip():
            return value.strip().rstrip("/")
    return default


def _provider_api_key(provider: str, *, extra_env_names: tuple[str, ...] = ()) -> str | None:
    for env_name in (f"{provider}_API_KEY", *extra_env_names):
        value = os.getenv(env_name)
        if value and value.strip():
            return value.strip()
    return None


def _ensure_api_key(model: str) -> None:
    load_dotenv()
    env_names = _api_key_envs(model)
    if not env_names:
        raise UnsupportedLLMModel(model)
    if not any(os.getenv(env_name) for env_name in env_names):
        raise MissingLLMApiKey(model, " or ".join(env_names))


def _api_key_env(model: str) -> str | None:
    env_names = _api_key_envs(model)
    return env_names[0] if env_names else None


def _api_key_envs(model: str) -> tuple[str, ...]:
    normalized = model.lower()
    if normalized.startswith("deepseek") or normalized.startswith("deepseek/"):
        return ("DEEPSEEK_API_KEY",)
    if normalized.startswith("kimi") or normalized.startswith("moonshot/"):
        return ("MOONSHOT_API_KEY",)
    if normalized.startswith("openrouter"):
        return ("OPENROUTER_API_KEY",)
    return ()


def _temperature_for_model(model: str) -> int:
    return 1 if _api_key_env(model) == "MOONSHOT_API_KEY" else 0


def _is_openrouter_qwen(model: str) -> bool:
    return model.lower() == "openrouter-qwen3.6-35b-a3b"


def _completion_params_for_model(model: str, *, max_tokens: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"temperature": _temperature_for_model(model)}
    if _is_openrouter_qwen(model):
        # Greedy decoding (temperature=0) makes this Qwen model fall into a deterministic
        # repetition loop on math-heavy figures — e.g. it emits ``$\boldsymbol{\text{ }``
        # endlessly until it exhausts the output budget, so ``finish_reason='length'`` and
        # instructor raises ``IncompleteOutputException``. The Qwen model card recommends a
        # presence penalty to break such loops; retries alone never help because greedy
        # decoding reproduces the identical loop every attempt.
        params["presence_penalty"] = 1.5
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    extra_body = _extra_body_for_model(model)
    if extra_body:
        params["extra_body"] = extra_body
    return params


def _extra_body_for_model(model: str) -> dict[str, Any]:
    if _is_openrouter_qwen(model):
        return {"reasoning": {"effort": "none", "exclude": True}}
    return {}


def load_dotenv(path: str | Path | None = None) -> bool:
    dotenv_path = Path(path) if path is not None else _default_dotenv_path()
    if dotenv_path is None or not dotenv_path.exists():
        return False
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)
    return True


def _default_dotenv_path() -> Path | None:
    override = os.getenv("BOOKWIKI_DOTENV_PATH")
    if override:
        return Path(override)
    for parent in (Path.cwd(), *Path.cwd().parents):
        candidate = parent / ".env"
        if candidate.exists():
            return candidate
    repo_candidate = Path(__file__).resolve().parents[2] / ".env"
    return repo_candidate if repo_candidate.exists() else None


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").lstrip()
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return None
    return key, _parse_dotenv_value(value.strip())


def _parse_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return _strip_unquoted_comment(value).strip()


def _strip_unquoted_comment(value: str) -> str:
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _parse_structured_response(response: Any, output_model: type[BaseModel]) -> BaseModel:
    parsed = _extract_message_field(response, "parsed")
    if parsed is not None:
        return output_model.model_validate(parsed)

    content = _extract_message_field(response, "content")
    if isinstance(content, output_model):
        return content
    if isinstance(content, dict):
        return output_model.model_validate(content)
    if not isinstance(content, str):
        msg = f"LLM response did not include JSON content for {output_model.__name__}"
        raise ValueError(msg)
    return output_model.model_validate(json.loads(_strip_json_fence(content)))


def _extract_message_field(response: Any, field_name: str) -> Any:
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        return message.get(field_name) if isinstance(message, dict) else None

    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    return getattr(message, field_name, None)


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text


def _strip_mdx_fence(content: str) -> str:
    text = content.strip()
    match = re.match(
        r"^```(?:mdx|markdown|md)?\s*(.*?)\s*```$",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else text


_VALID_ESCAPE_NEXT = set('\\"/bfnrtu')


def _truncate_for_log(text: str, *, limit: int = 600) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[+{len(text) - limit} chars]"


def _repair_json_escapes(content: str) -> str:
    """Repair only isolated invalid backslash escapes while preserving valid pairs."""
    out: list[str] = []
    i, n = 0, len(content)
    while i < n:
        ch = content[i]
        if ch == "\\":
            nxt = content[i + 1] if i + 1 < n else ""
            if nxt in _VALID_ESCAPE_NEXT:
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _content_is_valid_json(text: str) -> bool:
    try:
        json.loads(_strip_json_fence(text))
        return True
    except (ValueError, TypeError):
        return False


def _repair_response_json_escapes(response: Any) -> Any:
    if isinstance(response, dict):
        choices = response.get("choices", [])
    else:
        choices = getattr(response, "choices", [])
    for choice in choices or []:
        if isinstance(choice, dict):
            message = choice.get("message")
        else:
            message = getattr(choice, "message", None)
        if message is None:
            continue
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and not _content_is_valid_json(content):
                message["content"] = _repair_json_escapes(content)
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str) and not _content_is_valid_json(content):
            message.content = _repair_json_escapes(content)
    return response


def _extract_draft_payload(user: str) -> Any | None:
    marker = "Draft JSON:\n"
    if marker not in user:
        return None
    draft = user.split(marker, 1)[1].strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```", draft, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        draft = fenced.group(1).strip()
    else:
        draft = draft.split("\n\nReturn only", 1)[0].strip()
    return json.loads(draft)


def _extract_draft_document(user: str) -> str | None:
    marker = "Draft Document:\n"
    if marker not in user:
        return None
    draft = user.split(marker, 1)[1].strip()
    fenced = re.match(
        r"^```(?:mdx|markdown|md)?\s*(.*?)\s*```",
        draft,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1).strip()
    return draft.split("\n\nReturn only", 1)[0].strip()


def _assistant_tool_message(message: Any) -> dict[str, Any]:  # pragma: no cover - real API path
    if hasattr(message, "model_dump"):
        return message.model_dump()
    tool_calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments},
        }
        for call in getattr(message, "tool_calls", None) or []
    ]
    return {
        "role": "assistant",
        "content": getattr(message, "content", "") or "",
        "tool_calls": tool_calls,
    }


async def _run_tool_call(
    call: Any, tool_executor: ToolExecutor
) -> dict[str, Any]:  # pragma: no cover - real API path
    tool_name = call.function.name
    raw_args = call.function.arguments or "{}"
    try:
        args = json.loads(raw_args)
    except (json.JSONDecodeError, TypeError) as exc:
        # Surface malformed tool arguments back to the model instead of silently
        # running with ``{}`` (which makes run_plot fail with an empty-code error the
        # model can't diagnose, burning tool rounds). Returning the parse error lets
        # it correct the call.
        _LOG.warning("tool call name=%s has invalid JSON arguments: %s", tool_name, exc)
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(
                {"ok": False, "error": f"invalid JSON tool arguments: {exc}"},
                ensure_ascii=False,
            ),
        }
    _LOG.info("tool call name=%s args=%s", tool_name, _truncate_for_log(raw_args))
    result = tool_executor(tool_name, args)
    if inspect.isawaitable(result):
        result = await result
    payload = json.dumps(result, ensure_ascii=False, default=str)
    _LOG.info("tool result name=%s result=%s", tool_name, _truncate_for_log(payload))
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": payload,
    }


def _messages(
    *, system: str, user: str, image_paths: Sequence[str | Path] | None = None
) -> list[dict[str, Any]]:
    if not image_paths:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    content: list[dict[str, Any]] = [{"type": "text", "text": user}]
    for image_path in image_paths:
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(image_path)}})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def _image_data_url(image_path: str | Path) -> str:
    path = Path(image_path)
    mime_type = mimetypes.guess_type(path.name)[0]
    if mime_type is None or not mime_type.startswith("image/"):
        mime_type = "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_instructor_client(router: Any, *, on_usage: Callable[..., None] | None = None) -> Any:
    try:
        import instructor
    except Exception as exc:  # pragma: no cover - exercised only without optional extra
        raise LLMRuntimeUnavailable(
            "Instructor is required for structured LLM calls; install the runtime extra"
        ) from exc

    async def repaired_acompletion(*args: Any, **kwargs: Any) -> Any:
        response = await router.acompletion(*args, **kwargs)
        # Record usage on the raw response BEFORE instructor consumes it into the
        # validated model (instructor.create returns the model, not the response, so
        # this wrapper is the only place the JSON path can see token/cost usage).
        if on_usage is not None:
            on_usage(response, model=kwargs.get("model"))
        return _repair_response_json_escapes(response)

    return instructor.from_litellm(repaired_acompletion, mode=instructor.Mode.JSON)


def _get_attr_or_key(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _extract_usage(response: Any) -> tuple[int, int]:
    """Return ``(prompt_tokens, completion_tokens)`` from a dict or object response."""
    usage = _get_attr_or_key(response, "usage")
    if usage is None:
        return 0, 0
    prompt = _get_attr_or_key(usage, "prompt_tokens") or 0
    completion = _get_attr_or_key(usage, "completion_tokens") or 0
    try:
        return int(prompt), int(completion)
    except (TypeError, ValueError):
        return 0, 0


def _extract_cost(response: Any) -> float:
    """Best-effort response cost from LiteLLM/OpenRouter metadata.

    LiteLLM usually stores computed Router cost in ``_hidden_params.response_cost``.
    OpenRouter responses routed through ``openai/`` can instead expose the provider
    cost under ``usage.cost`` while the hidden cost remains ``0.0`` because the
    model is not in LiteLLM's built-in price map. Prefer any positive hidden cost,
    then provider-reported usage cost, then LiteLLM's own fallback calculator.
    """
    hidden = _get_attr_or_key(response, "_hidden_params")
    if isinstance(hidden, dict):
        cost = hidden.get("response_cost")
        if cost is not None:
            try:
                parsed = float(cost)
            except (TypeError, ValueError):
                pass
            else:
                if parsed > 0:
                    return parsed
    usage = _get_attr_or_key(response, "usage")
    usage_cost = _get_attr_or_key(usage, "cost") if usage is not None else None
    if usage_cost is not None:
        try:
            parsed = float(usage_cost)
        except (TypeError, ValueError):
            pass
        else:
            if parsed > 0:
                return parsed * OPENROUTER_USD_TO_CNY
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response))
    except Exception:  # pragma: no cover - cost is best-effort, never fatal
        _LOG.debug("could not determine response cost; recording 0")
        return 0.0
