from __future__ import annotations

import os

import pytest

from bookwiki.scheduler.llm import LiteLLMRuntime, MissingLLMApiKey, load_dotenv
from bookwiki.schemas.chapter import ChapterResult


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
    monkeypatch.setattr("instructor.from_litellm", lambda completion: client)
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
    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
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
    monkeypatch.setattr("instructor.from_litellm", lambda completion: client)
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
