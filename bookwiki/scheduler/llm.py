from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class LLMRuntime(Protocol):
    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
    ) -> BaseModel:
        """Return a validated structured result from a real or explicitly injected LLM."""


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


class LiteLLMRuntime:
    def __init__(self, router: Any | None = None) -> None:
        self.router = router

    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
    ) -> BaseModel:
        _ensure_api_key(model)
        router = self.router if self.router is not None else build_router()
        self.router = router
        response = await router.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=output_model,
            temperature=0,
        )
        return _parse_structured_response(response, output_model)


class TestLLMRuntime:
    """Explicit fake runtime for subprocess smoke tests."""

    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
    ) -> BaseModel:
        draft = _extract_draft_payload(user)
        if draft is None:
            msg = f"test runtime needs a Draft JSON block for {output_model.__name__}"
            raise ValueError(msg)
        return output_model.model_validate(draft)


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
    draft = user.split(marker, 1)[1].split("\n\nReturn only", 1)[0].strip()
    return json.loads(draft)


def build_instructor_client(router: Any) -> None:
    return None
