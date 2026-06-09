from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bookwiki.agents import QualityCheckAgent
from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.checkers.mdx_validator import validate_mdx
from bookwiki.scheduler.cache import run_with_cache
from bookwiki.scheduler.config import BookConfig


@dataclass(frozen=True)
class ArtifactIssue:
    kind: Literal["mdx", "citation", "quality"]
    message: str
    quote: str = ""
    explanation: str = ""


async def validate_artifact(
    *,
    body_md: str,
    kind: str,
    allowed_refs: set[str],
    cfg: BookConfig,
) -> list[ArtifactIssue]:
    """Validate a generated chapter/concept body before rendering.

    Quality checking is deliberately default-off; when disabled this function
    performs no LLM calls.
    """
    issues: list[ArtifactIssue] = []

    for error in validate_mdx(body_md):
        issues.append(ArtifactIssue(kind="mdx", message=error))

    for ref_id in SOURCE_REF_RE.findall(body_md):
        if ref_id not in allowed_refs:
            issues.append(
                ArtifactIssue(
                    kind="citation",
                    message=f"body_md cites unknown source_ref {ref_id}",
                )
            )

    if not cfg.generation.get("qualityCheck"):
        return issues

    result = await run_with_cache(
        QualityCheckAgent,
        {
            "owner_task_id": f"{kind}:inline-validation",
            "title": kind,
            "body_md": body_md,
            "language": cfg.language,
            "kind": kind,
        },
        model=cfg.model_for("quality_check"),
        cache_dir=cfg.cache_dir / "tasks",
        runtime=cfg.llm_runtime,
    )
    for finding in result.result.findings:
        if finding.category != "language_leak":
            continue
        issues.append(
            ArtifactIssue(
                kind="quality",
                message=f"{finding.quote}: {finding.explanation}",
                quote=finding.quote,
                explanation=finding.explanation,
            )
        )
    return issues
