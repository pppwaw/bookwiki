from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from bookwiki.scheduler.budget_guard import BudgetExceeded
from bookwiki.scheduler.llm import (
    OPENROUTER_USD_TO_CNY,
    LiteLLMRuntime,
    MissingLLMApiKey,
    _extract_cost,
    _extract_usage,
    _is_rate_limit_error,
    _model_list,
    _repair_json_escapes,
    _repair_response_json_escapes,
    build_instructor_client,
    load_dotenv,
)
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.source import VisionCaptionItem


class _Router:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def acompletion(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": self.content}}]}


class _InstructorClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        output_model = kwargs["response_model"]
        context = kwargs.get("context")
        return output_model.model_validate(self.payload, context=context)


def test_load_dotenv_reads_project_env_without_overriding_existing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=from-file\n"
        "MOONSHOT_API_KEY='moonshot file value'\n"
        "DEEPSEEK_API_BASE_URL=https://deepseek.example/v1\n"
        "MOONSHOT_API_BASE_URL=https://moonshot.example/v1\n"
        "EXISTING=from-file\n"
        "# comment\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_BASE_URL", raising=False)
    monkeypatch.delenv("MOONSHOT_API_BASE_URL", raising=False)
    monkeypatch.setenv("EXISTING", "from-env")

    loaded = load_dotenv(env_path)

    assert loaded is True
    assert os.environ["DEEPSEEK_API_KEY"] == "from-file"
    assert os.environ["MOONSHOT_API_KEY"] == "moonshot file value"
    assert os.environ["DEEPSEEK_API_BASE_URL"] == "https://deepseek.example/v1"
    assert os.environ["MOONSHOT_API_BASE_URL"] == "https://moonshot.example/v1"
    assert os.environ["EXISTING"] == "from-env"


@pytest.mark.asyncio
async def test_build_instructor_client_uses_json_mode_and_repairs_invalid_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_from_litellm(completion, *, mode):
        calls.append({"completion": completion, "mode": mode})
        return object()

    router = _Router(r'{"x":"$\sigma$"}')
    monkeypatch.setattr("instructor.from_litellm", fake_from_litellm)

    build_instructor_client(router)

    assert calls[0]["mode"].value == "json_mode"
    response = await calls[0]["completion"](model="deepseek-v4-pro", messages=[])
    assert response["choices"][0]["message"]["content"] == r'{"x":"$\\sigma$"}'


def test_moonshot_model_list_uses_official_api_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.delenv("MOONSHOT_API_BASE_URL", raising=False)
    monkeypatch.delenv("MOONSHOT_API_BASE", raising=False)

    moonshot = next(item for item in _model_list() if item["model_name"] == "kimi-k2.6")

    assert moonshot["litellm_params"]["api_base"] == "https://api.moonshot.cn/v1"


def test_model_list_uses_configured_provider_api_base_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-key")
    monkeypatch.setenv("DEEPSEEK_API_BASE_URL", "https://deepseek.example/v1/")
    monkeypatch.setenv("MOONSHOT_API_BASE_URL", "https://moonshot.example/v1/")

    models = {item["model_name"]: item for item in _model_list()}

    assert models["deepseek-v4-pro"]["litellm_params"]["api_base"] == (
        "https://deepseek.example/v1"
    )
    assert models["deepseek-v4-flash"]["litellm_params"]["api_base"] == (
        "https://deepseek.example/v1"
    )
    assert models["kimi-k2.6"]["litellm_params"]["api_base"] == ("https://moonshot.example/v1")


def test_model_list_uses_short_api_base_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.delenv("DEEPSEEK_API_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://deepseek-alias.example/v1")
    monkeypatch.setattr("bookwiki.scheduler.llm.load_dotenv", lambda *args, **kwargs: False)

    deepseek = next(item for item in _model_list() if item["model_name"] == "deepseek-v4-pro")

    assert deepseek["litellm_params"]["api_base"] == "https://deepseek-alias.example/v1"


def test_openrouter_model_list_uses_openrouter_env_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("BOOKWIKI_CHAT_API_KEY", "chat-key")
    monkeypatch.setenv("OPENROUTER_API_BASE_URL", "https://openrouter.example/v1/")
    monkeypatch.setenv("BOOKWIKI_CHAT_BASE_URL", "https://chat.example/v1/")
    monkeypatch.setattr("bookwiki.scheduler.llm.load_dotenv", lambda *args, **kwargs: False)

    models = {item["model_name"]: item for item in _model_list()}
    openrouter = models["openrouter-qwen3.6-35b-a3b"]["litellm_params"]

    assert openrouter["model"] == "openai/qwen/qwen3.6-35b-a3b"
    assert openrouter["api_key"] == "openrouter-key"
    assert openrouter["api_base"] == "https://openrouter.example/v1"
    assert "input_cost_per_token" in openrouter
    assert "output_cost_per_token" in openrouter


def test_repair_keeps_valid_latex_json() -> None:
    content = r'{"q":"$\\max\\{x\\}$"}'

    repaired = _repair_json_escapes(content)

    assert json.loads(repaired)["q"] == r"$\max\{x\}$"


def test_repair_fixes_underescaped_latex() -> None:
    content = '{"q":"$\\max$"}'

    repaired = _repair_json_escapes(content)

    assert json.loads(repaired)["q"] == r"$\max$"


def test_repair_idempotent_on_valid_json() -> None:
    content = r'{"q":"$\\frac\\{\\partial f\\}\\{\\partial x\\}$"}'

    repaired = _repair_json_escapes(content)

    assert repaired == content
    assert _repair_json_escapes(repaired) == content


def test_response_repair_skips_valid_content() -> None:
    content = r'{"q":"$\\max\\{x\\}$"}'
    response = {"choices": [{"message": {"content": content}}]}

    _repair_response_json_escapes(response)

    assert response["choices"][0]["message"]["content"] == content


def test_response_repair_fixes_invalid_content() -> None:
    response = {"choices": [{"message": {"content": '{"q":"$\\max$"}'}}]}

    _repair_response_json_escapes(response)

    content = response["choices"][0]["message"]["content"]
    assert json.loads(content)["q"] == r"$\max$"


def test_response_repair_handles_object_message_content() -> None:
    message = SimpleNamespace(content='{"q":"$\\sigma$"}')
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])

    _repair_response_json_escapes(response)

    assert json.loads(message.content)["q"] == r"$\sigma$"


def test_repair_handles_newline_quote_and_unicode_escapes() -> None:
    content = r'{"q":"line\n\"quoted\"\u03bc"}'

    repaired = _repair_json_escapes(content)

    assert repaired == content
    assert json.loads(repaired)["q"] == 'line\n"quoted"μ'


@pytest.mark.asyncio
async def test_litellm_runtime_requires_deepseek_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("BOOKWIKI_DOTENV_PATH", "C:/definitely/missing/bookwiki.env")
    runtime = LiteLLMRuntime(router=_Router("{}"))

    with pytest.raises(MissingLLMApiKey, match="DEEPSEEK_API_KEY"):
        await runtime.generate(
            model="deepseek-v4-pro",
            output_model=ChapterResult,
            system="system",
            user="user",
        )


@pytest.mark.asyncio
async def test_litellm_runtime_requires_kimi_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("BOOKWIKI_DOTENV_PATH", "C:/definitely/missing/bookwiki.env")
    runtime = LiteLLMRuntime(router=_Router("{}"))

    with pytest.raises(MissingLLMApiKey, match="MOONSHOT_API_KEY"):
        await runtime.generate(
            model="kimi-k2.6",
            output_model=ChapterResult,
            system="system",
            user="user",
        )


@pytest.mark.asyncio
async def test_litellm_runtime_requires_openrouter_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("BOOKWIKI_CHAT_API_KEY", "chat-key")
    monkeypatch.setenv("BOOKWIKI_DOTENV_PATH", "C:/definitely/missing/bookwiki.env")
    runtime = LiteLLMRuntime(router=_Router("{}"))

    with pytest.raises(MissingLLMApiKey, match="OPENROUTER_API_KEY"):
        await runtime.generate(
            model="openrouter-qwen3.6-35b-a3b",
            output_model=ChapterResult,
            system="system",
            user="user",
        )


@pytest.mark.asyncio
async def test_litellm_runtime_revalidates_with_context_after_client_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    payload = {
        "chapter_id": "chapter-6",
        "title": "Point Estimation",
        "body_md": "# Point Estimation\n\nBody",
        "concepts": ["point estimation"],
        "citations": [{"ref_id": "Week-10-p001", "quote": "method of moments"}],
        "owner_task_id": "chapter-6:chapter",
    }
    client = _InstructorClient(payload)
    monkeypatch.setattr("instructor.from_litellm", lambda completion, **_: client)
    router = _Router("{}")
    runtime = LiteLLMRuntime(router=router)

    result = await runtime.generate(
        model="deepseek-v4-pro",
        output_model=ChapterResult,
        system="system",
        user="source contains {{ not_a_template _ }}",
        context={"allowed_citation_refs": {"Week-10-p001"}},
        max_retries=2,
    )

    assert result.title == "Point Estimation"
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "deepseek-v4-pro"
    assert client.calls[0]["response_model"] is ChapterResult
    assert "context" not in client.calls[0]
    assert client.calls[0]["max_retries"] == 2
    assert client.calls[0]["temperature"] == 0
    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "source contains {{ not_a_template _ }}"},
    ]


@pytest.mark.asyncio
async def test_litellm_runtime_sends_image_paths_as_multimodal_content(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"image-bytes")
    payload = {
        "block_id": "source-p001-b001",
        "caption_md": "A source figure.",
        "source_ref": "source-p001",
        "confidence": 0.8,
    }
    client = _InstructorClient(payload)
    monkeypatch.setattr("instructor.from_litellm", lambda completion, **_: client)
    runtime = LiteLLMRuntime(router=_Router("{}"))

    result = await runtime.generate(
        model="kimi-k2.6",
        output_model=VisionCaptionItem,
        system="system",
        user="describe the figure",
        image_paths=[image_path],
    )

    encoded = base64.b64encode(b"image-bytes").decode("ascii")
    assert result.caption_md == "A source figure."
    assert client.calls[0]["temperature"] == 1
    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe the figure"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                },
            ],
        },
    ]


@pytest.mark.asyncio
async def test_openrouter_qwen_disables_reasoning_for_structured_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    payload = {
        "block_id": "source-p001-b001",
        "caption_md": "A source figure.",
        "source_ref": "source-p001",
        "confidence": 0.8,
    }
    client = _InstructorClient(payload)
    monkeypatch.setattr("instructor.from_litellm", lambda completion, **_: client)
    runtime = LiteLLMRuntime(router=_Router("{}"))

    result = await runtime.generate(
        model="openrouter-qwen3.6-35b-a3b",
        output_model=VisionCaptionItem,
        system="system",
        user="describe the figure",
        max_tokens=4096,
    )

    assert result.caption_md == "A source figure."
    assert client.calls[0]["temperature"] == 0
    assert client.calls[0]["extra_body"] == {"reasoning": {"effort": "none", "exclude": True}}
    # Greedy decoding loops on math figures; a presence penalty breaks the repetition,
    # and the caller-scaled max_tokens caps a runaway so it fails fast and cheap.
    assert client.calls[0]["presence_penalty"] == 1.5
    assert client.calls[0]["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_non_qwen_model_has_no_presence_penalty_or_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    payload = {
        "chapter_id": "chapter-1",
        "title": "Intro",
        "body_md": "# Intro\n\nBody",
        "owner_task_id": "chapter-1:chapter",
    }
    client = _InstructorClient(payload)
    monkeypatch.setattr("instructor.from_litellm", lambda completion, **_: client)
    runtime = LiteLLMRuntime(router=_Router("{}"))

    await runtime.generate(
        model="deepseek-v4-pro",
        output_model=ChapterResult,
        system="system",
        user="write the chapter",
    )

    assert "presence_penalty" not in client.calls[0]
    assert "max_tokens" not in client.calls[0]
    assert "extra_body" not in client.calls[0]


@pytest.mark.asyncio
async def test_litellm_runtime_loads_dotenv_before_key_check(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("BOOKWIKI_DOTENV_PATH", str(env_path))
    payload = {
        "chapter_id": "chapter-6",
        "title": "Point Estimation",
        "body_md": "# Point Estimation\n\nBody",
        "concepts": ["point estimation"],
        "citations": [{"ref_id": "Week-10-p001", "quote": "method of moments"}],
        "owner_task_id": "chapter-6:chapter",
    }
    client = _InstructorClient(payload)
    monkeypatch.setattr("instructor.from_litellm", lambda completion, **_: client)
    router = _Router("{}")
    runtime = LiteLLMRuntime(router=router)

    result = await runtime.generate(
        model="deepseek-v4-pro",
        output_model=ChapterResult,
        system="system",
        user="user",
    )

    assert result.chapter_id == "chapter-6"
    assert os.environ["DEEPSEEK_API_KEY"] == "from-dotenv"


class _Tiny(BaseModel):
    value: str


class _RouterRateLimit(Exception):
    """Stand-in for litellm's RouterRateLimitError (matched by name/message)."""


class _FlakyClient:
    def __init__(self, payload: dict[str, object], *, fail_times: int, exc: Exception) -> None:
        self.payload = payload
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    async def create(self, **kwargs: object) -> object:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        output_model = kwargs["response_model"]
        return output_model.model_validate(self.payload, context=kwargs.get("context"))


def test_is_rate_limit_error_walks_cause_chain() -> None:
    assert _is_rate_limit_error(_RouterRateLimit("boom"))
    assert _is_rate_limit_error(
        RuntimeError("No deployments available for selected model, Try again in 5 seconds")
    )
    nested = ValueError("wrapper")
    nested.__cause__ = _RouterRateLimit("inner rate limit")
    assert _is_rate_limit_error(nested)
    assert not _is_rate_limit_error(ValueError("just a bad value"))


@pytest.mark.asyncio
async def test_generate_backs_off_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("bookwiki.scheduler.llm.asyncio.sleep", _fake_sleep)
    client = _FlakyClient(
        {"value": "ok"},
        fail_times=2,
        exc=_RouterRateLimit("No deployments available, Try again in 5 seconds"),
    )
    runtime = LiteLLMRuntime(router=object())
    runtime.client = client

    result = await runtime.generate(
        model="deepseek-v4-pro", output_model=_Tiny, system="s", user="u"
    )

    assert isinstance(result, _Tiny) and result.value == "ok"
    assert client.calls == 3  # 2 rate-limit failures + 1 success
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]  # exponential backoff


@pytest.mark.asyncio
async def test_generate_reraises_non_rate_limit_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    sleeps: list[float] = []
    monkeypatch.setattr(
        "bookwiki.scheduler.llm.asyncio.sleep",
        lambda delay: sleeps.append(delay),
    )
    client = _FlakyClient({"value": "ok"}, fail_times=2, exc=ValueError("schema mismatch"))
    runtime = LiteLLMRuntime(router=object())
    runtime.client = client

    with pytest.raises(ValueError, match="schema mismatch"):
        await runtime.generate(model="deepseek-v4-pro", output_model=_Tiny, system="s", user="u")

    assert client.calls == 1  # no retries for non-rate-limit errors
    assert sleeps == []


@pytest.mark.asyncio
async def test_concurrent_first_calls_build_single_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    build_calls = {"router": 0, "client": 0}

    def fake_build_router() -> object:
        build_calls["router"] += 1
        return object()

    def fake_build_instructor_client(
        router: object, *, on_usage: object = None
    ) -> _InstructorClient:
        build_calls["client"] += 1
        return _InstructorClient({"value": "ok"})

    monkeypatch.setattr("bookwiki.scheduler.llm.build_router", fake_build_router)
    monkeypatch.setattr(
        "bookwiki.scheduler.llm.build_instructor_client", fake_build_instructor_client
    )
    runtime = LiteLLMRuntime()

    results = await asyncio.gather(
        *(
            runtime.generate(model="deepseek-v4-pro", output_model=_Tiny, system="s", user="u")
            for _ in range(10)
        )
    )

    assert all(isinstance(r, _Tiny) and r.value == "ok" for r in results)
    assert build_calls["router"] == 1  # lock collapses concurrent first-build into one
    assert build_calls["client"] == 1


def _usage_response(prompt: int, completion: int, cost: float) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        _hidden_params={"response_cost": cost},
        model="deepseek-v4-pro",
    )


def test_record_usage_accumulates_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    runtime = LiteLLMRuntime(router=object())
    response = _usage_response(100, 50, 0.0123)

    with caplog.at_level(logging.INFO, logger="bookwiki.scheduler.llm"):
        runtime._record_usage(response)
        runtime._record_usage(response)

    assert runtime.total_prompt_tokens == 200
    assert runtime.total_completion_tokens == 100
    assert runtime.total_cost_cny == pytest.approx(0.0246)
    assert any("llm usage" in record.getMessage() for record in caplog.records)


def test_record_usage_enforces_budget() -> None:
    runtime = LiteLLMRuntime(router=object(), max_cost_cny=0.05)

    runtime._record_usage(_usage_response(10, 10, 0.04))  # under budget, fine
    with pytest.raises(BudgetExceeded, match="budget exceeded"):
        runtime._record_usage(_usage_response(10, 10, 0.02))  # crosses 0.05


def test_record_usage_unlimited_when_budget_non_positive() -> None:
    runtime = LiteLLMRuntime(router=object(), max_cost_cny=0)

    for _ in range(5):
        runtime._record_usage(_usage_response(10, 10, 100.0))

    assert runtime.total_cost_cny == pytest.approx(500.0)  # never raised


def test_extract_usage_and_cost_tolerate_missing_fields() -> None:
    assert _extract_usage({"choices": []}) == (0, 0)
    assert _extract_usage(SimpleNamespace()) == (0, 0)
    # No hidden params and no litellm cost computable -> 0.0, never raises.
    assert _extract_cost({"choices": []}) == 0.0


def test_extract_cost_uses_openrouter_usage_cost_when_hidden_cost_is_zero() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(cost=0.001),
        _hidden_params={"response_cost": 0.0},
    )

    assert _extract_cost(response) == pytest.approx(0.001 * OPENROUTER_USD_TO_CNY)


@pytest.mark.asyncio
async def test_run_tool_call_returns_error_on_invalid_json_args() -> None:
    from bookwiki.scheduler.llm import _run_tool_call

    calls: list[tuple[str, dict]] = []

    def executor(name: str, args: dict) -> dict:
        calls.append((name, args))
        return {"ok": True}

    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="run_plot", arguments="{not valid json"),
    )

    result = await _run_tool_call(call, executor)

    content = json.loads(result["content"])
    assert content["ok"] is False
    assert "invalid JSON tool arguments" in content["error"]
    assert result["tool_call_id"] == "call-1"
    assert calls == []  # executor body never runs on malformed arguments
