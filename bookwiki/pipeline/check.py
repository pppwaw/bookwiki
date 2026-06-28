from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bookwiki.agents import (
    MdxEditRepairAgent,
    ReviewAgent,
)
from bookwiki.checkers.mdx_validator import (
    mdx_validator_available,
    validate_mdx_many,
)
from bookwiki.checkers.quiz_extractor import (
    QuizExtractError,
    extract_inline_quizzes,
)
from bookwiki.pipeline._shared import (
    _EXAM_PAGE_FILENAME,
    _LOG,
    State,
    _agent_result,
    _cache_dir,
    _json_model,
    _mdx_link_exists,
    _rel,
)
from bookwiki.scheduler.cache import run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text


def _inline_quiz_answer_issues(text: str, stem: str) -> list[Issue]:
    """Warn if a rendered inline quiz item's answer is not among its choice ids.

    Generate-time ``sanitize_inline_quizzes`` already enforces this on authored knowledge
    quizzes; this is a macro-stage safety net for residue (e.g. a section whose body was not
    MDX-parseable when sanitized and was only healed later). Emitted as a ``warning`` because
    there is no macro repair path for inline (body-authored) quizzes — it surfaces in the
    check report without wedging the repair loop.
    """
    try:
        blocks = extract_inline_quizzes(text)
    except QuizExtractError:
        return []  # a real parse failure is already reported as MDX_PARSE_ERROR
    issues: list[Issue] = []
    for block in blocks:
        for child in block.get("children", []):
            if child.get("kind") != "item":
                continue
            choice_ids = {choice.get("id") for choice in child.get("choices", [])}
            if child.get("answer") not in choice_ids:
                issues.append(
                    Issue(
                        severity="warning",
                        code="INLINE_QUIZ_ANSWER_NOT_IN_CHOICES",
                        message=f"{stem}.mdx inline quiz answer is not among its choices",
                        owner_task_id=f"{stem}:quiz",
                    )
                )
    return issues


# A code fence whose body holds an MDX component (e.g. ```quiz around <QuizBlock>): valid
# Markdown, so it passes MDX compilation, and the wrapped ``<QuizBlock`` keeps the page from
# tripping MISSING_QUIZ — yet the site's syntax highlighter throws on the unknown language at
# render time. We catch it deterministically and unwrap it. ``mermaid`` is the one legitimate
# component-free fence and is exempt.
_COMPONENT_FENCE_RE = re.compile(
    r"^```[ \t]*([A-Za-z0-9_-]+)?[^\n]*\n(.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
# Require the component tag to start its own line: a real wrapped component sits on its own line,
# whereas a string like ``print("<QuizBlock>")`` inside a legit code sample must not trip this.
_MDX_COMPONENT_RE = re.compile(r"^[ \t]*<(?:Quiz[A-Za-z]*|BookFigure|PreviewLink)\b", re.MULTILINE)
_ALLOWED_FENCE_LANGS = {"mermaid"}
# Codes whose repair edits the rendered ``.mdx`` in place (then re-``check``), rather than
# re-running a source agent via ``integrate``.
_MDX_ROUTE_CODES = {"MDX_PARSE_ERROR", "ILLEGAL_CODE_FENCE"}


def _illegal_component_fence_issues(text: str, owner_task_id: str) -> list[Issue]:
    """Flag code fences that wrap MDX components (e.g. ```quiz around ``<QuizBlock>``)."""
    issues: list[Issue] = []
    for match in _COMPONENT_FENCE_RE.finditer(text):
        lang = (match.group(1) or "").lower()
        if lang in _ALLOWED_FENCE_LANGS:
            continue
        if _MDX_COMPONENT_RE.search(match.group(2)):
            issues.append(
                Issue(
                    severity="error",
                    code="ILLEGAL_CODE_FENCE",
                    message=(
                        f"code fence ```{lang or '(no lang)'} wraps an MDX component; "
                        "remove the fence so the component renders"
                    ),
                    owner_task_id=owner_task_id,
                )
            )
    return issues


def _strip_illegal_component_fences(text: str) -> str:
    """Remove component-wrapping code fences, keeping the component body (deterministic repair)."""

    def _unwrap(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").lower()
        if lang not in _ALLOWED_FENCE_LANGS and _MDX_COMPONENT_RE.search(match.group(2)):
            return match.group(2)
        return match.group(0)

    return _COMPONENT_FENCE_RE.sub(_unwrap, text)


def _suspicious_phrases(markdown: str) -> list[str]:
    phrases = ["ignore previous instructions", "system prompt", "developer message"]
    lower = markdown.lower()
    return [phrase for phrase in phrases if phrase in lower]


def _allowed_source_refs(state: State, cfg: BookConfig) -> set[str]:
    refs: set[str] = set()
    for rel_path in state.get("sources_md", []):
        path = cfg.book_dir / rel_path
        if path.exists():
            refs.update(re.findall(r"source_ref:\s*([^\s>]+)", path.read_text(encoding="utf-8")))
    if refs:
        return refs
    for paths in state.get("agent_results", {}).values():
        for rel_path in paths.values():
            refs.update(_iter_citation_refs(read_json(cfg.book_dir / rel_path)))
    return refs


def _iter_citation_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        if "ref_id" in value and "quote" in value:
            refs.append(str(value["ref_id"]))
        for item in value.values():
            refs.extend(_iter_citation_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_iter_citation_refs(item))
    return refs


def _render_check_report_md(report: CheckReport) -> str:
    lines = ["# Check Report", "", f"Status: `{report.status}`", ""]
    if not report.issues:
        lines.append("No issues.")
        return "\n".join(lines) + "\n"
    for issue in report.issues:
        lines.append(
            f"- `{issue.severity}` `{issue.code}` owner `{issue.owner_task_id}`: {issue.message}"
        )
    return "\n".join(lines) + "\n"


def _owner_output_payload(owner_task_id: str, state: State, cfg: BookConfig) -> dict[str, Any]:
    path = _owner_artifact_path(owner_task_id, state, cfg)
    if path is None:
        return {}
    return read_json(path)


def _owner_artifact_path(owner_task_id: str, state: State, cfg: BookConfig) -> Path | None:
    if owner_task_id.startswith("concept:"):
        for name, rel_path in state.get("concept_pages", {}).items():
            path = cfg.book_dir / rel_path
            payload = read_json(path, default={})
            owner = str(_agent_result(payload).get("owner_task_id") or f"concept:{name}")
            if owner == owner_task_id:
                return path
        return None
    chapter_id, _, kind = owner_task_id.partition(":")
    rel_path = state.get("agent_results", {}).get(chapter_id, {}).get(kind)
    return cfg.book_dir / rel_path if rel_path else None


def _artifact_owner_task_id(ch_id: str, kind: str, payload: dict[str, Any]) -> str:
    result = _agent_result(payload)
    owner = result.get("owner_task_id")
    return str(owner) if owner else f"{ch_id}:{kind}"


def _apply_repair(
    owner_task_id: str, issues: list[dict[str, Any]], state: State, cfg: BookConfig
) -> dict[str, Any] | None:
    """Apply deterministic repairs, preferring DROP over fabrication.

    Returns an audit record of what was removed (or ``None`` if nothing changed).
    Earlier this collapsed every invalid citation ref onto one valid ref and rewrote
    quiz answers / empty cards with placeholder text - both silently corrupted
    content (wrong attribution, wrong answers, English stubs in a zh-CN book). We now
    delete the offending citation/quiz-item/card instead, so the artifact stays
    truthful and the loss is recorded for review.
    """
    path = _owner_artifact_path(owner_task_id, state, cfg)
    if path is None:
        return None
    payload = read_json(path)
    result = payload.get("result", payload)
    codes = {str(issue.get("code")) for issue in issues}
    allowed_refs = _allowed_source_refs(state, cfg)
    actions: dict[str, Any] = {}
    if "UNKNOWN_SOURCE_REF" in codes and allowed_refs:
        dropped = _drop_invalid_citations(result, allowed_refs)
        if dropped:
            actions["dropped_citations"] = dropped
    _, _, kind = owner_task_id.partition(":")
    if kind == "quiz" and "QUIZ_ANSWER_NOT_IN_CHOICES" in codes:
        dropped_quiz = _drop_invalid_quiz_items(result)
        if dropped_quiz:
            actions["dropped_quiz_items"] = dropped_quiz
    elif kind == "card" and "EMPTY_CARD_SIDE" in codes:
        dropped_cards = _drop_empty_cards(result)
        if dropped_cards:
            actions["dropped_cards"] = dropped_cards
    write_json(path, payload)
    if actions:
        return {"owner_task_id": owner_task_id, **actions}
    return None


def _is_invalid_citation(elem: Any, allowed_refs: set[str]) -> bool:
    return (
        isinstance(elem, dict)
        and "ref_id" in elem
        and "quote" in elem
        and str(elem["ref_id"]) not in allowed_refs
    )


def _drop_invalid_citations(value: Any, allowed_refs: set[str]) -> list[str]:
    """Recursively remove citation dicts whose ``ref_id`` is not allowed.

    Returns the list of removed ``ref_id`` values. Unlike the previous
    collapse-to-first-ref behaviour, this never reassigns a citation to a different
    (wrong) source - an unverifiable citation is dropped, not silently re-attributed.
    """
    removed: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in list(node.items()):
                if isinstance(item, list):
                    kept = [elem for elem in item if not _is_invalid_citation(elem, allowed_refs)]
                    removed.extend(
                        str(elem["ref_id"])
                        for elem in item
                        if _is_invalid_citation(elem, allowed_refs)
                    )
                    node[key] = kept
                    for elem in kept:
                        walk(elem)
                else:
                    walk(item)
        elif isinstance(node, list):
            for elem in node:
                walk(elem)

    walk(value)
    return removed


def _drop_invalid_quiz_items(result: dict[str, Any]) -> list[str]:
    """Drop quiz items whose answer is not among the choices.

    Returns short descriptions of the removed items. ``QuizResult.items`` has no
    minimum length, so deleting down to zero is schema-valid.
    """
    items = result.get("items", [])
    kept_items: list[Any] = []
    removed: list[str] = []
    for item in items:
        choices = [str(choice) for choice in item.get("choices", [])]
        if bool(choices) and str(item.get("answer", "")) not in choices:
            removed.append(str(item.get("question", ""))[:60])
            continue
        kept_items.append(item)
    if removed:
        result["items"] = kept_items
    return removed


def _drop_empty_cards(result: dict[str, Any]) -> list[str]:
    """Drop cards with an empty front or back. ``CardResult.items`` has no minimum."""
    items = result.get("items", [])
    kept: list[Any] = []
    removed: list[str] = []
    for index, item in enumerate(items, start=1):
        if not str(item.get("front", "")).strip() or not str(item.get("back", "")).strip():
            removed.append(f"card {index}")
            continue
        kept.append(item)
    if removed:
        result["items"] = kept
    return removed


def _require_mdx_validator(cfg: BookConfig) -> None:
    """Fail loudly if the MDX validator is unavailable, unless explicitly waived.

    When Node / the bundled validator's ``node_modules`` are missing, ``validate_mdx``
    silently returns ``[]`` ("no errors") - which would disable every inline AND macro
    MDX check at once and let broken MDX reach the rendered site. The ``check`` stage is
    the last gate, so it refuses to run blind. ``generation.allowMissingMdxValidator``
    is the escape hatch for environments that knowingly have no Node (degrades to a
    single loud error instead of aborting).
    """
    if mdx_validator_available():
        return
    if cfg.generation.get("allowMissingMdxValidator"):
        _LOG.error(
            "mdx validator unavailable but allowMissingMdxValidator=true; "
            "MDX compile checks are DISABLED for this run"
        )
        return
    msg = (
        "mdx validator unavailable: install Node and run `npm install` in "
        "tools/mdx-validate (or set generation.allowMissingMdxValidator=true to "
        "skip MDX checks). Refusing to run check blind."
    )
    raise RuntimeError(msg)


def _site_typecheck_issues(cfg: BookConfig) -> list[Issue]:
    mode = str(cfg.generation.get("siteTypeCheck", "auto") or "auto").lower()
    if mode in {"off", "false", "0", "disabled"}:
        return []
    required = mode in {"required", "on", "true", "1"}
    if mode not in {"auto", "required", "on", "true", "1"}:
        _LOG.warning("unknown generation.siteTypeCheck=%s; treating as auto", mode)
        required = False

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        message = "site type check skipped: pnpm is unavailable"
        if required:
            return [
                Issue(
                    severity="error",
                    code="SITE_TYPECHECK_UNAVAILABLE",
                    message=message,
                    owner_task_id="site:typecheck",
                )
            ]
        _LOG.info("%s", message)
        return []

    # site is the single source of truth: integrate already scaffolded the framework and rendered
    # content into it, so check validates it in place — no per-round materialize. Reuse installed
    # deps (preserved across runs); only install when node_modules is genuinely absent.
    site_dir = cfg.site_dir
    env = _site_typecheck_env(cfg)
    if not (site_dir / "node_modules").exists():
        try:
            install_proc = subprocess.run(  # noqa: S603 - fixed argv, project-local package manager
                [pnpm, "install"],
                cwd=site_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return [
                Issue(
                    severity="error",
                    code="SITE_TYPECHECK_ERROR",
                    message=f"site dependency install failed to run: {exc}",
                    owner_task_id="site:typecheck",
                )
            ]
        if install_proc.returncode != 0:
            output = _redact_site_typecheck_output(
                "\n".join(
                    part
                    for part in [install_proc.stdout.strip(), install_proc.stderr.strip()]
                    if part
                )
            )
            if len(output) > 4000:
                output = output[:4000] + "..."
            return [
                Issue(
                    severity="error",
                    code="SITE_TYPECHECK_ERROR",
                    message=(
                        f"site dependency install failed (exit {install_proc.returncode}): {output}"
                    ),
                    owner_task_id="site:typecheck",
                )
            ]

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, project-local package script
            [pnpm, "run", "types:check"],
            cwd=site_dir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [
            Issue(
                severity="error",
                code="SITE_TYPECHECK_ERROR",
                message=f"site type check failed to run: {exc}",
                owner_task_id="site:typecheck",
            )
        ]
    if proc.returncode != 0:
        output = _redact_site_typecheck_output(
            "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
        )
        if len(output) > 4000:
            output = output[:4000] + "..."
        return [
            Issue(
                severity="error",
                code="SITE_TYPECHECK_ERROR",
                message=f"site type check failed (exit {proc.returncode}): {output}",
                owner_task_id="site:typecheck",
            )
        ]
    # types:check passed → run a real build to surface runtime render errors (e.g. ShikiError on an
    # unknown code-fence language, component render failures) that a type-only check cannot see.
    return _site_build_issues(site_dir, env, pnpm)


def _site_build_issues(site_dir: Path, env: dict[str, str], pnpm: str) -> list[Issue]:
    """Run ``pnpm run build`` and report a SITE_BUILD_ERROR if the production build fails."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, project-local package script
            [pnpm, "run", "build"],
            cwd=site_dir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [
            Issue(
                severity="error",
                code="SITE_BUILD_ERROR",
                message=f"site build failed to run: {exc}",
                owner_task_id="site:build",
            )
        ]
    if proc.returncode == 0:
        return []
    output = _redact_site_typecheck_output(
        "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
    )
    if len(output) > 4000:
        output = output[:4000] + "..."
    return [
        Issue(
            severity="error",
            code="SITE_BUILD_ERROR",
            message=f"site build failed (exit {proc.returncode}): {output}",
            owner_task_id="site:build",
        )
    ]


def _site_typecheck_env(cfg: BookConfig) -> dict[str, str]:
    env: dict[str, str] = {
        "BOOKWIKI_SITE_LANGUAGE": cfg.language,
        "NODE_OPTIONS": "--max-old-space-size=4096",
    }
    for key in ("PATH", "HOME", "TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _redact_site_typecheck_output(output: str) -> str:
    redacted = output
    for key, value in os.environ.items():
        if _looks_sensitive_env_key(key) and value and len(value) >= 4:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _looks_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))


async def check_node(state: State, cfg: BookConfig) -> State:
    _require_mdx_validator(cfg)
    issues: list[Issue] = []
    for raw_issue in state.get("generation_issues", []):
        if isinstance(raw_issue, dict):
            issues.append(Issue.model_validate(raw_issue))
    for raw_issue in state.get("concept_generation_issues", []):
        if isinstance(raw_issue, dict):
            issues.append(Issue.model_validate(raw_issue))
    _LOG.info(
        "check: seed issues from generate=%d concept_pages=%d",
        len(state.get("generation_issues", [])),
        len(state.get("concept_generation_issues", [])),
    )
    chapter_mdx_files = sorted((cfg.content_dir / "chapters").rglob("*.mdx"))
    concept_mdx_files = sorted((cfg.content_dir / "concepts").glob("*.mdx"))
    if not (cfg.content_dir / "index.mdx").exists():
        issues.append(
            Issue(
                severity="error",
                code="MISSING_CONTENT_INDEX",
                message="content/docs/index.mdx was not generated",
                owner_task_id="content:index",
            )
        )
    chapter_texts = {path: path.read_text(encoding="utf-8") for path in chapter_mdx_files}
    # One Node process per batch instead of a cold start per file (~550 files → ~100s).
    chapter_mdx = validate_mdx_many(
        [(str(path), chapter_texts[path]) for path in chapter_mdx_files]
    )
    for path in chapter_mdx_files:
        text = chapter_texts[path]
        # owner_task_id carries the chapter-relative path (e.g. ``Chapter-19-X/index``) rather
        # than the bare stem: 30 chapters all share ``index.mdx``/``exam.mdx``, so a stem-based id
        # collapses them onto one target and ``_target_mdx_path`` would only ever repair the first.
        rel_id = path.relative_to(cfg.content_dir / "chapters").with_suffix("").as_posix()
        mdx_errors = chapter_mdx.get(str(path), [])
        for error in mdx_errors:
            issues.append(
                Issue(
                    severity="error",
                    code="MDX_PARSE_ERROR",
                    message=f"{path.name} fails MDX compilation: {error}",
                    owner_task_id=f"{rel_id}:chapter",
                )
            )
        issues.extend(_illegal_component_fence_issues(text, f"{rel_id}:chapter"))
        if not text.startswith("---\n"):
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_FRONTMATTER",
                    message=f"{path.name} has no YAML frontmatter",
                    owner_task_id=f"{rel_id}:chapter",
                )
            )
        # QuizBlock / Anki Cards / Sources are pedagogical sections that only teaching-chapter
        # pages carry. Exam pages (``exam.mdx``) are structural and legitimately omit them, so we
        # skip these checks there (otherwise every exam page is a permanent false positive).
        # The three are also reported as ``warning`` rather than ``error``: none has a deterministic
        # repair path (ReviewAgent only emits advice, nothing re-fills the missing section), so an
        # ``error`` would only burn futile repair rounds. We record them instead of trying to fix.
        if path.name != _EXAM_PAGE_FILENAME:
            if "<QuizBlock" not in text:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MISSING_QUIZ",
                        message=f"{path.name} has no QuizBlock",
                        owner_task_id=f"{rel_id}:quiz",
                    )
                )
            elif not mdx_errors:
                issues.extend(_inline_quiz_answer_issues(text, rel_id))
            if "## Anki Cards" not in text:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MISSING_ANKI",
                        message=f"{path.name} has no Anki Cards section",
                        owner_task_id=f"{rel_id}:card",
                    )
                )
            if "## Sources" not in text:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MISSING_SOURCES",
                        message=f"{path.name} has no Sources section",
                        owner_task_id=f"{rel_id}:chapter",
                    )
                )
        for phrase in _suspicious_phrases(text):
            issues.append(
                Issue(
                    severity="warning",
                    code="SUSPICIOUS_INSTRUCTION",
                    message=f"{path.name} contains suspicious instruction text: {phrase}",
                    owner_task_id=f"{rel_id}:chapter",
                )
            )
        for target in re.findall(r"\]\((?!https?://|mailto:|#)([^)]+)\)", text):
            if not _mdx_link_exists(path.parent, target):
                issues.append(
                    Issue(
                        severity="error",
                        code="BROKEN_LINK",
                        message=f"{path.name} links to missing target {target}",
                        owner_task_id=f"{rel_id}:chapter",
                    )
                )

    concept_texts = {path: path.read_text(encoding="utf-8") for path in concept_mdx_files}
    concept_mdx = validate_mdx_many(
        [(str(path), concept_texts[path]) for path in concept_mdx_files]
    )
    for path in concept_mdx_files:
        for error in concept_mdx.get(str(path), []):
            issues.append(
                Issue(
                    severity="error",
                    code="MDX_PARSE_ERROR",
                    message=f"{path.name} fails MDX compilation: {error}",
                    owner_task_id=f"concept-mdx:{path.stem}",
                )
            )
        issues.extend(
            _illegal_component_fence_issues(concept_texts[path], f"concept-mdx:{path.stem}")
        )

    allowed_refs = _allowed_source_refs(state, cfg)
    _LOG.info(
        "check: chapter_mdx=%d concept_mdx=%d allowed_refs=%d",
        len(chapter_mdx_files),
        len(concept_mdx_files),
        len(allowed_refs),
    )
    for ch_id, paths in state.get("agent_results", {}).items():
        for kind, rel_path in paths.items():
            payload = read_json(cfg.book_dir / rel_path)
            for ref_id in _iter_citation_refs(payload):
                if allowed_refs and ref_id not in allowed_refs:
                    issues.append(
                        Issue(
                            severity="error",
                            code="UNKNOWN_SOURCE_REF",
                            message=f"{rel_path} cites unknown source_ref {ref_id}",
                            owner_task_id=_artifact_owner_task_id(ch_id, kind, payload),
                        )
                    )
        quiz = _agent_result(read_json(cfg.book_dir / paths["quiz"]))
        for index, item in enumerate(quiz.get("items", []), start=1):
            choices = [str(choice) for choice in item.get("choices", [])]
            answer = str(item.get("answer", ""))
            if answer not in choices:
                issues.append(
                    Issue(
                        severity="error",
                        code="QUIZ_ANSWER_NOT_IN_CHOICES",
                        message=f"{ch_id} quiz item {index} answer is not in choices",
                        owner_task_id=f"{ch_id}:quiz",
                    )
                )
        card = _agent_result(read_json(cfg.book_dir / paths["card"]))
        for index, item in enumerate(card.get("items", []), start=1):
            if not str(item.get("front", "")).strip() or not str(item.get("back", "")).strip():
                issues.append(
                    Issue(
                        severity="error",
                        code="EMPTY_CARD_SIDE",
                        message=f"{ch_id} card item {index} has an empty side",
                        owner_task_id=f"{ch_id}:card",
                    )
                )
    for name, rel_path in state.get("concept_pages", {}).items():
        payload = read_json(cfg.book_dir / rel_path)
        owner = str(_agent_result(payload).get("owner_task_id") or f"concept:{name}")
        for ref_id in _iter_citation_refs(payload):
            if allowed_refs and ref_id not in allowed_refs:
                issues.append(
                    Issue(
                        severity="error",
                        code="UNKNOWN_SOURCE_REF",
                        message=f"{rel_path} cites unknown source_ref {ref_id}",
                        owner_task_id=owner,
                    )
                )
    issues.extend(_site_typecheck_issues(cfg))
    status = "needs_repair" if issues else "passed"
    report = CheckReport(status=status, issues=issues)
    logs_dir = ensure_dir(cfg.work_dir / "logs")
    report_path = write_json(logs_dir / "check-report.json", report.model_dump(mode="json"))
    write_json(cfg.work_dir / "check-report.json", report.model_dump(mode="json"))
    write_text(logs_dir / "check-report.md", _render_check_report_md(report))
    by_severity: dict[str, int] = {}
    for issue in issues:
        key = str(issue.severity)
        by_severity[key] = by_severity.get(key, 0) + 1
    _LOG.info(
        "check: done status=%s issues=%d by_severity=%s report=%s",
        status,
        len(issues),
        by_severity,
        _rel(report_path, cfg.book_dir),
    )
    return {
        "check_report": _rel(report_path, cfg.book_dir),
        "repair_targets": report.repair_targets,
    }


async def repair_node(state: State, cfg: BookConfig) -> State:
    targets = state.get("repair_targets", [])
    if not targets:
        _LOG.info("repair: no targets, nothing to do")
        return {"repair_targets": []}
    # Round budget lives on the (run-scoped, non-checkpointed) cfg, NOT in the LangGraph state:
    # it must persist across the in-run integrate->check->repair loop to bound it, but must NOT
    # ride the checkpoint into a later run (where stale counters would make every target look
    # exhausted on the first pass). A fresh process => fresh cfg => empty budget. We mutate the
    # dict in place so each loop iteration sees the previous one's increments.
    rounds = cfg._repair_rounds
    out_dir = ensure_dir(cfg.work_dir / "repairs")
    outputs = []
    repair_actions: list[dict[str, Any]] = []
    exhausted: list[dict[str, Any]] = []
    mdx_edited: list[str] = []
    report = read_json(cfg.book_dir / state.get("check_report", "work/logs/check-report.json"))
    max_rounds = int(cfg.generation.get("maxRepairRounds", 1) or 1)
    _LOG.info(
        "repair: targets=%d rounds_state=%d max_rounds=%d",
        len(targets),
        len(rounds),
        max_rounds,
    )

    target_set = set(targets)
    issues_by_target: dict[str, list[dict[str, Any]]] = {}
    for issue in report.get("issues", []):
        owner = issue.get("owner_task_id")
        if owner in target_set:
            issues_by_target.setdefault(owner, []).append(issue)

    def _codes(t: str) -> set[str]:
        return {str(issue.get("code")) for issue in issues_by_target.get(t, [])}

    charged: set[str] = set()
    seen_exhausted: set[str] = set()

    def _charge(target: str) -> bool:
        """Spend one repair round for ``target`` (idempotent per invocation).

        Returns False — and records the target as exhausted exactly once — when its round
        budget is used up. A mixed target's source side (round N) and MDX side (round N+1,
        post-integrate) are charged in *different* invocations, so its two phases each cost a
        round; the in-place phase is never starved by the source phase within one round.
        """
        if target in charged:
            return True
        if int(rounds.get(target, 0)) >= max_rounds:
            if target not in seen_exhausted:
                seen_exhausted.add(target)
                exhausted.append(
                    {
                        "owner_task_id": target,
                        "codes": sorted(_codes(target)),
                        "rounds": int(rounds.get(target, 0)),
                    }
                )
                _LOG.warning(
                    "repair exhausted target=%s codes=%s rounds=%d (kept unrepaired)",
                    target,
                    sorted(_codes(target)),
                    int(rounds.get(target, 0)),
                )
            return False
        rounds[target] = int(rounds.get(target, 0)) + 1
        charged.add(target)
        return True

    # --- Phase 1: source rewrites (route back to ``integrate``). Any target carrying a code that
    # is NOT an in-place MDX fix rewrites its source artifact. We handle those here and DEFER
    # every in-place MDX edit this round: the ``integrate`` these rewrites force does a full
    # rmtree + re-render, which would clobber an edit applied now (and waste its repair budget on
    # a fix that gets re-rendered away). Deferred MDX targets return via ``check`` after integrate.
    source_targets = [t for t in targets if _codes(t) - _MDX_ROUTE_CODES]
    for target in source_targets:
        codes = _codes(target)
        if not _charge(target):
            continue
        target_issues = issues_by_target.get(target, [])
        _LOG.info(
            "repair: target=%s route=review codes=%s round=%d/%d",
            target,
            sorted(codes),
            rounds[target],
            max_rounds,
        )
        # A target with no in-place MDX code is regenerated by ReviewAgent; a mixed target only
        # has its source side dropped here (its MDX side waits for the post-integrate round).
        if not (codes & _MDX_ROUTE_CODES):
            result = await run_with_cache(
                ReviewAgent,
                {
                    "owner_task_id": target,
                    "issues": target_issues,
                    "previous_output": _owner_output_payload(target, state, cfg),
                },
                model=cfg.model_for("review"),
                cache_dir=_cache_dir(cfg),
                force=True,
                runtime=cfg.llm_runtime,
            )
            path = write_json(
                out_dir / f"{target.replace(':', '-')}.json", _json_model(result.result)
            )
            outputs.append(_rel(path, cfg.book_dir))
        action = _apply_repair(target, target_issues, state, cfg)
        if action is not None:
            repair_actions.append(action)
            _LOG.warning("repair applied destructive fix (content removed): %s", action)

    # A source rewrite this round means ``integrate`` is coming. Only do in-place MDX edits when
    # nothing rewrote source (no integrate to clobber them) — otherwise defer to the next round.
    source_changed = bool(outputs or repair_actions)

    # --- Phase 2: in-place ``.mdx`` edits (route to ``check``, NOT ``integrate``). ---
    if not source_changed:
        for target in (t for t in targets if _codes(t) & _MDX_ROUTE_CODES):
            codes = _codes(target)
            if not _charge(target):
                continue
            _LOG.info(
                "repair: target=%s route=mdx codes=%s round=%d/%d",
                target,
                sorted(codes),
                rounds[target],
                max_rounds,
            )
            if await _repair_mdx_file(target, issues_by_target.get(target, []), state, cfg):
                mdx_edited.append(target)

    if repair_actions:
        write_json(
            ensure_dir(cfg.work_dir / "logs") / "repair-actions.json",
            {"actions": repair_actions},
        )
    if exhausted:
        write_json(
            ensure_dir(cfg.work_dir / "logs") / "repair-exhausted.json",
            {"exhausted": exhausted},
        )
    _LOG.info(
        "repair: done review_repaired=%d destructive_applied=%d mdx_repaired=%d "
        "deferred_mdx=%d exhausted=%d",
        len(outputs),
        len(repair_actions),
        len(mdx_edited),
        sum(1 for t in targets if _codes(t) & _MDX_ROUTE_CODES) if source_changed else 0,
        len(exhausted),
    )
    return {
        "repairs": outputs,
        "mdx_edited": mdx_edited,
        # Phase 1 (review/destructive) rewrote source artifacts -> ``integrate`` must re-render;
        # phase 2 (in-place ``.mdx`` edits) only needs ``check`` to re-validate. The two are never
        # mixed in one round, so an integrate never clobbers an in-place edit.
        "repair_artifact_changed": source_changed,
        "repair_targets": [],
        # NOTE: the round budget (cfg._repair_rounds) is intentionally NOT returned into the
        # LangGraph state — it is run-scoped cfg bookkeeping, never checkpointed (see above).
        "repair_exhausted": exhausted,
    }


def _target_mdx_path(target: str, cfg: BookConfig) -> Path | None:
    """Map a ``check`` ``owner_task_id`` back to the rendered ``.mdx`` file it validated.

    ``check`` derives ``<chapter-rel-path>:<kind>`` from each chapter file (e.g.
    ``Chapter-19-X/index:chapter``) and ``concept-mdx:<stem>`` from each concept file, so the
    reverse mapping joins that relative path under ``chapters/``. The relative path is unique per
    file, which is what makes per-file repair possible (a bare stem would alias all ``index.mdx``).
    """
    if target.startswith("concept-mdx:"):
        path = cfg.content_dir / "concepts" / f"{target.partition(':')[2]}.mdx"
        return path if path.exists() else None
    rel_id = target.rsplit(":", 1)[0]
    path = cfg.content_dir / "chapters" / f"{rel_id}.mdx"
    return path if path.exists() else None


async def _repair_mdx_file(
    target: str, target_issues: list[dict[str, Any]], state: State, cfg: BookConfig
) -> bool:
    """Fix MDX compile errors by editing the rendered ``.mdx`` file IN PLACE.

    Feeds the actual ``.mdx`` bytes (the ones ``check`` compiled) plus the ``MDX_PARSE_ERROR``
    diagnostics to ``MdxEditRepairAgent``, then writes the repaired text back. The caller
    routes to ``check`` (NOT ``integrate``) so the edit is not clobbered by re-rendering;
    on a later ``integrate`` the file is regenerated from source and re-repaired. Returns
    whether the file changed.
    """
    path = _target_mdx_path(target, cfg)
    if path is None:
        return False
    text = path.read_text(encoding="utf-8")
    codes = {str(issue.get("code")) for issue in target_issues}
    changed = False
    # Deterministically unwrap component-wrapping code fences (e.g. ```quiz around <QuizBlock>)
    # before handing anything to the LLM — this needs no model and never touches the component.
    if "ILLEGAL_CODE_FENCE" in codes:
        unwrapped = _strip_illegal_component_fences(text)
        if unwrapped != text:
            text = unwrapped
            changed = True
    mdx_errors = [
        str(issue.get("message"))
        for issue in target_issues
        if str(issue.get("code")) == "MDX_PARSE_ERROR"
    ]
    if mdx_errors:
        result = await run_with_cache(
            MdxEditRepairAgent,
            {"mdx": text, "mdx_errors": mdx_errors, "language": cfg.language, "doc_label": target},
            model=cfg.model_for("mdx_repair"),
            cache_dir=_cache_dir(cfg),
            force=True,
            runtime=cfg.llm_runtime,
        )
        repaired = result.result.mdx
        if repaired != text:
            text = repaired
            changed = True
    if not changed:
        return False
    path.write_text(text, encoding="utf-8")
    return True


__all__ = [
    "_inline_quiz_answer_issues",
    "_COMPONENT_FENCE_RE",
    "_MDX_COMPONENT_RE",
    "_ALLOWED_FENCE_LANGS",
    "_MDX_ROUTE_CODES",
    "_illegal_component_fence_issues",
    "_strip_illegal_component_fences",
    "_suspicious_phrases",
    "_allowed_source_refs",
    "_iter_citation_refs",
    "_render_check_report_md",
    "_owner_output_payload",
    "_owner_artifact_path",
    "_artifact_owner_task_id",
    "_apply_repair",
    "_is_invalid_citation",
    "_drop_invalid_citations",
    "_drop_invalid_quiz_items",
    "_drop_empty_cards",
    "_require_mdx_validator",
    "_site_typecheck_issues",
    "_site_build_issues",
    "_site_typecheck_env",
    "_redact_site_typecheck_output",
    "_looks_sensitive_env_key",
    "check_node",
    "repair_node",
    "_target_mdx_path",
    "_repair_mdx_file",
]
