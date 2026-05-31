from __future__ import annotations

import base64
import os

import pytest

from bookwiki.scheduler.llm import (
    LiteLLMRuntime,
    MissingLLMApiKey,
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
async def test_litellm_runtime_uses_instructor_client_with_context(
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
        user="user",
        context={"allowed_citation_refs": {"Week-10-p001"}},
        max_retries=2,
    )

    assert result.title == "Point Estimation"
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "deepseek-v4-pro"
    assert client.calls[0]["response_model"] is ChapterResult
    assert client.calls[0]["context"] == {"allowed_citation_refs": {"Week-10-p001"}}
    assert client.calls[0]["max_retries"] == 2
    assert client.calls[0]["temperature"] == 0
    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
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
