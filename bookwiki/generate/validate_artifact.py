from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from bookwiki.agents import QualityCheckAgent
from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.checkers.mdx_validator import validate_mdx
from bookwiki.integrator.markdown_renderers import normalize_mdx_for_validation
from bookwiki.scheduler.cache import run_with_cache
from bookwiki.scheduler.config import BookConfig


@dataclass(frozen=True)
class ArtifactIssue:
    kind: Literal["mdx", "citation", "quality"]
    message: str
    quote: str = ""
    explanation: str = ""


# Phrases where the prose stops teaching and instead *narrates the source
# document itself* ("源材料中…", "源文中…", "原文指出…", "教材中…"). These leak the
# raw source ("源") to the reader, who should only ever see the author's own
# teaching voice. The patterns are deliberately specific so the many circuit
# terms that contain "源" (电压源 / 电流源 / 电源 / 源电流 …) never match.
_SOURCE_META_REFERENCE_RE = re.compile(
    "|".join(
        (
            r"源材料",
            r"源文档",
            r"源文本",
            r"源文(?=[中里所的提指写给说表最])",
            r"原文(?=[中里所的提指写给说表最明])",
            r"文档中(?=对此|有|的描述|描述|提到|给出|写道|指出|说明)",
            r"书中(?=提到|描述|写道|指出|给出|有清晰|清晰)",
            r"教材中",
            r"课本中",
            r"原书(?=[中里的指提写给])",
            r"如(?:源材料|源文|原文)所(?:述|示|说|写)",
            r"根据(?:源材料|源文|原文|教材|课本|原书)",
            r"源中(?=对此|有|提到|描述|给出)",
        )
    )
)

_META_REFERENCE_SENTENCE_END = "。！？\n"
_META_REFERENCE_SPAN_BOUNDARY = "。！？\n；（）"

_META_REFERENCE_EXPLANATION = (
    "正文出现了指向源文档/源材料/原文/教材的元叙述（如“源材料中…”“源文中…”“原文指出…”），"
    "这会把资料“源”暴露给读者。请删掉这类出处叙述，用你自己的讲解口吻改写该片段，"
    "保留其中的技术结论、定义与公式；不要照抄源文里的英文原句，需要时用目标语言转述。"
)


def _meta_reference_span(body_md: str, start: int, end: int) -> str:
    """Return the exact substring to rewrite: the enclosing （…） if any, else the sentence.

    The returned text is always a verbatim slice of ``body_md`` so the content
    rewrite agent (which only edits spans it can find byte-for-byte) can locate it.
    """
    open_paren = body_md.rfind("（", 0, start)
    if open_paren != -1 and body_md.find("）", open_paren, start) == -1:
        close_paren = body_md.find("）", end)
        if close_paren != -1 and close_paren - open_paren <= 600:
            return body_md[open_paren : close_paren + 1]
    left = start
    while left > 0 and body_md[left - 1] not in _META_REFERENCE_SPAN_BOUNDARY:
        left -= 1
    while left < start and body_md[left] in " \t":
        left += 1
    right = end
    while right < len(body_md) and body_md[right] not in _META_REFERENCE_SENTENCE_END:
        right += 1
    if right < len(body_md):
        right += 1
    return body_md[left:right]


def _source_meta_reference_issues(body_md: str) -> list[ArtifactIssue]:
    """Deterministically flag prose that narrates the source document itself.

    Runs regardless of the (default-off) ``qualityCheck`` flag, so source
    meta-references are caught — and routed into the existing inline content
    rewrite repair — even when no quality LLM is configured to run.
    """
    issues: list[ArtifactIssue] = []
    seen: set[str] = set()
    for match in _SOURCE_META_REFERENCE_RE.finditer(body_md):
        quote = _meta_reference_span(body_md, match.start(), match.end()).strip()
        if not quote or quote in seen:
            continue
        seen.add(quote)
        issues.append(
            ArtifactIssue(
                kind="quality",
                message=(f"body_md 直接叙述了资料出处（“{match.group(0)}”），应改写为自有讲解口吻"),
                quote=quote,
                explanation=_META_REFERENCE_EXPLANATION,
            )
        )
    return issues


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

    validation_body = normalize_mdx_for_validation(body_md)
    for error in validate_mdx(validation_body):
        issues.append(ArtifactIssue(kind="mdx", message=error))

    for ref_id in SOURCE_REF_RE.findall(body_md):
        if ref_id not in allowed_refs:
            issues.append(
                ArtifactIssue(
                    kind="citation",
                    message=f"body_md cites unknown source_ref {ref_id}",
                )
            )

    # Deterministic source meta-reference detection runs regardless of the
    # (default-off) quality LLM, so "源材料中…"/"源文中…" leaks are caught and
    # repaired even when qualityCheck is disabled.
    issues.extend(_source_meta_reference_issues(body_md))

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
