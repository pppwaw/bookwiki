from __future__ import annotations

import base64
import inspect
import json
import mimetypes
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

# A tool executor maps ``(tool_name, arguments)`` to a JSON-serialisable result.
# It may be sync or async; ``generate_with_tools`` awaits awaitable results.
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


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


class LiteLLMRuntime:
    def __init__(self, router: Any | None = None) -> None:
        self.router = router
        self.client: Any | None = None

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
    ) -> BaseModel:
        _ensure_api_key(model)
        router = self.router if self.router is not None else build_router()
        self.router = router
        client = self.client if self.client is not None else build_instructor_client(router)
        self.client = client
        result = await client.create(
            model=model,
            response_model=output_model,
            messages=_messages(system=system, user=user, image_paths=image_paths),
            max_retries=max_retries,
            temperature=_temperature_for_model(model),
        )
        if isinstance(result, output_model):
            return output_model.model_validate(result.model_dump(mode="json"), context=context)
        return output_model.model_validate(result, context=context)

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
        router = self.router if self.router is not None else build_router()
        self.router = router
        messages = _messages(system=system, user=user, image_paths=image_paths)
        for _round in range(max_tool_rounds):
            response = await router.acompletion(
                model=model,
                messages=messages,
                tools=list(tools),
                tool_choice="auto",
                temperature=_temperature_for_model(model),
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                break
            messages.append(_assistant_tool_message(message))
            for call in tool_calls:
                messages.append(await _run_tool_call(call, tool_executor))
        else:
            raise ToolLoopExceeded(model, max_tool_rounds)

        client = self.client if self.client is not None else build_instructor_client(router)
        self.client = client
        result = await client.create(
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
            temperature=_temperature_for_model(model),
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
    ) -> BaseModel:
        draft = _extract_draft_payload(user)
        if draft is None:
            msg = f"test runtime needs a Draft JSON block for {output_model.__name__}"
            raise ValueError(msg)
        return output_model.model_validate(draft, context=context)

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


def build_runtime() -> LLMRuntime:
    load_dotenv()
    if os.getenv("BOOKWIKI_TEST_LLM") == "1":
        return TestLLMRuntime()
    return LiteLLMRuntime()


def build_router() -> Any:
    load_dotenv()
    try:
        from litellm import Router
    except Exception as exc:  # pragma: no cover - exercised only without optional extra
        raise LLMRuntimeUnavailable(
            "LiteLLM is required for real LLM calls; install the runtime extra"
        ) from exc

    return Router(
        model_list=_model_list(),
        routing_strategy="usage-based-routing-v2",
        num_retries=3,
        retry_after=2,
        fallbacks=[{"deepseek-v4-pro": ["deepseek-v4-flash"]}],
    )


def _model_list() -> list[dict[str, Any]]:
    load_dotenv()
    return [
        {
            "model_name": "deepseek-v4-pro",
            "litellm_params": {
                "model": "deepseek/deepseek-v4-pro",
                "api_key": os.getenv("DEEPSEEK_API_KEY"),
            },
            "tpm": 200_000,
            "rpm": 60,
        },
        {
            "model_name": "deepseek-v4-flash",
            "litellm_params": {
                "model": "deepseek/deepseek-v4-flash",
                "api_key": os.getenv("DEEPSEEK_API_KEY"),
            },
            "tpm": 400_000,
            "rpm": 120,
        },
        {
            "model_name": "kimi-k2.6",
            "litellm_params": {
                "model": "moonshot/kimi-k2.6",
                "api_key": os.getenv("MOONSHOT_API_KEY"),
                "api_base": "https://api.moonshot.cn/v1",
            },
        },
    ]


def _ensure_api_key(model: str) -> None:
    load_dotenv()
    env_name = _api_key_env(model)
    if not env_name:
        raise UnsupportedLLMModel(model)
    if not os.getenv(env_name):
        raise MissingLLMApiKey(model, env_name)


def _api_key_env(model: str) -> str | None:
    normalized = model.lower()
    if normalized.startswith("deepseek") or normalized.startswith("deepseek/"):
        return "DEEPSEEK_API_KEY"
    if normalized.startswith("kimi") or normalized.startswith("moonshot/"):
        return "MOONSHOT_API_KEY"
    return None


def _temperature_for_model(model: str) -> int:
    return 1 if _api_key_env(model) == "MOONSHOT_API_KEY" else 0


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
    try:
        args = json.loads(call.function.arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        args = {}
    result = tool_executor(call.function.name, args)
    if inspect.isawaitable(result):
        result = await result
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": json.dumps(result, ensure_ascii=False, default=str),
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


def build_instructor_client(router: Any) -> Any:
    try:
        import instructor
    except Exception as exc:  # pragma: no cover - exercised only without optional extra
        raise LLMRuntimeUnavailable(
            "Instructor is required for structured LLM calls; install the runtime extra"
        ) from exc

    return instructor.from_litellm(router.acompletion, mode=instructor.Mode.JSON)
