from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StubRouter:
    model_list: list[dict[str, Any]]
    usage_logs: list[dict[str, Any]] = field(default_factory=list)

    async def acompletion(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.usage_logs.append({"cost_usd": 0.0, "tokens": 0})
        return {"choices": [{"message": {"content": "{}"}}]}


def build_router() -> Any:
    model_list = [
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
        {"model_name": "gemma-4", "litellm_params": {"model": "vertex_ai/gemma-4-it"}},
        {
            "model_name": "kimi-k2.6",
            "litellm_params": {
                "model": "moonshot/kimi-k2.6",
                "api_key": os.getenv("MOONSHOT_API_KEY"),
            },
        },
    ]
    try:
        from litellm import Router

        return Router(
            model_list=model_list,
            routing_strategy="usage-based-routing-v2",
            num_retries=3,
            retry_after=2,
            fallbacks=[{"deepseek-v4-pro": ["deepseek-v4-flash"]}],
        )
    except Exception:
        return StubRouter(model_list=model_list)


def build_instructor_client(router: Any) -> Any:
    try:
        import instructor

        return instructor.from_litellm(router.acompletion)
    except Exception:
        return None
