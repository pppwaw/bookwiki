from __future__ import annotations

import base64
import os

import pytest
from pydantic import BaseModel

from bookwiki.scheduler.llm import (
    LiteLLMRuntime,
    MissingLLMApiKey,
    _is_rate_limit_error,
    _model_list,
    build_instructor_client,
    load_dotenv,
)
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.source import VisionCaptionResult


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
        "EXISTING=from-file\n"
        "# comment\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("EXISTING", "from-env")

    loaded = load_dotenv(env_path)

    assert loaded is True
    assert os.environ["DEEPSEEK_API_KEY"] == "from-file"
    assert os.environ["MOONSHOT_API_KEY"] == "moonshot file value"
    assert os.environ["EXISTING"] == "from-env"


def test_build_instructor_client_uses_json_mode_for_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_from_litellm(completion, *, mode):
        calls.append({"completion": completion, "mode": mode})
        return object()

    router = _Router("{}")
    monkeypatch.setattr("instructor.from_litellm", fake_from_litellm)

    build_instructor_client(router)

    assert calls[0]["completion"] == router.acompletion
    assert calls[0]["mode"].value == "json_mode"


def test_moonshot_model_list_uses_official_api_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")

    moonshot = next(item for item in _model_list() if item["model_name"] == "kimi-k2.6")

    assert moonshot["litellm_params"]["api_base"] == "https://api.moonshot.cn/v1"


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
        "caption_md": "A source figure.",
        "key_points": [],
        "source_ref": "source-p001",
        "confidence": 0.8,
    }
    client = _InstructorClient(payload)
    monkeypatch.setattr("instructor.from_litellm", lambda completion, **_: client)
    runtime = LiteLLMRuntime(router=_Router("{}"))

    result = await runtime.generate(
        model="kimi-k2.6",
        output_model=VisionCaptionResult,
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
