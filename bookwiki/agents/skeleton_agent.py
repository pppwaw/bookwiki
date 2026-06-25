from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.concepts import brief_for as _brief_for
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.skeleton import BookSkeleton, CanonicalConcept


class SkeletonAgent:
    """Build the book-wide skeleton (glossary + chapter briefs) before generate.

    Input shape (built in ``build_skeleton_node``)::

        {
            "chapters": [
                {
                    "chapter_id": "chapter-1",
                    "title": "...",
                    "topics": ["..."],
                    "source_md": "...",          # full chapter source markdown
                    "source_refs": ["..."],
                },
                ...
            ],
            "language": "zh-CN",
            "book_notes": "...",
        }

    The agent returns a :class:`BookSkeleton`. Behaviour mirrors
    :class:`ConceptReconcileAgent`: a deterministic draft is built locally from
    ``topics`` and chapter ``title``/``topics`` (giving the LLM a structured
    starting point and giving ``TestLLMRuntime`` an offline result to echo);
    the LLM then enriches the glossary by scanning each chapter's ``source_md``
    for additional concepts the curated ``topics`` list missed, and converges
    on canonical names + first-occurrence ownership.
    """

    kind: ClassVar[str] = "skeleton_llm_v1"
    output_model: ClassVar[type[BookSkeleton]] = BookSkeleton
    model_key: ClassVar[str] = "skeleton"
    prompt_name: ClassVar[str] = "skeleton"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 BookWiki 的全书骨架（skeleton）agent。

你的任务，在章节内容生成之前，为整本书建立一份只读契约：

1. **glossary**（术语表）：列出整本书会涉及的全部概念。每个条目：
   - `canonical`：本书全程统一使用的规范名称（保持稳定、教学价值高）。
   - `aliases`：所有变体写法、缩写、同义词。
   - `first_chapter_id`：该概念首次出现的章节 id（按章节序最早）。
2. **alias_map**：把 `canonical`、每个 alias、以及它们的归一化形式（去掉空格/标点
   并小写）都映射到该概念的 `canonical`。
3. **chapter_briefs**：每一章一句话摘要（不超过 80 个字），用于让相邻章节生成时
   写出承上启下的过渡。
4. **chapter_order**：章节按全书阅读顺序的 `chapter_id` 列表。

策略要求：
- 草稿（Draft JSON）已基于已批准结构里的 topics 生成。请认真扫一遍每章
  `source_md`，把 topics 漏掉但确实在源文本里出现的重要概念加进 glossary，
  并给出合适的 canonical 名称、别名、首现章节归属。
- 同一概念如果在多章出现，`first_chapter_id` 必须是按章节序最早的那一章；其他
  章节后续将引用而非重新定义。
- 不要凭空发明源文本里不存在的概念。
- `chapter_briefs` 的句子应能让下一章作者准确知道上一章讲了什么、自己应当承接
  哪个主题。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> BookSkeleton:
        chapters = inp.get("chapters", []) if isinstance(inp, dict) else []

        glossary: list[CanonicalConcept] = []
        by_key: dict[str, CanonicalConcept] = {}
        chapter_briefs: dict[str, str] = {}
        chapter_order: list[str] = []

        for chapter in chapters:
            ch_id = str(chapter.get("chapter_id") or "").strip()
            if not ch_id:
                continue
            if ch_id not in chapter_order:
                chapter_order.append(ch_id)
            title = str(chapter.get("title") or "").strip()
            topics = [str(t).strip() for t in chapter.get("topics", []) if str(t).strip()]
            chapter_briefs[ch_id] = _brief_for(title, topics)

            for topic in topics:
                key = _concept_key(topic)
                if not key:
                    continue
                existing = by_key.get(key)
                if existing is None:
                    entry = CanonicalConcept(
                        canonical=topic,
                        aliases=[],
                        first_chapter_id=ch_id,
                    )
                    by_key[key] = entry
                    glossary.append(entry)
                elif topic != existing.canonical and topic not in existing.aliases:
                    existing.aliases.append(topic)

        alias_map = _build_alias_map(glossary)
        draft = BookSkeleton(
            glossary=glossary,
            alias_map=alias_map,
            chapter_briefs=chapter_briefs,
            chapter_order=chapter_order,
        )

        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=BookSkeleton,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return BookSkeleton.model_validate(result)


def _build_alias_map(glossary: list[CanonicalConcept]) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for entry in glossary:
        canonical = entry.canonical
        for variant in (canonical, *entry.aliases):
            alias_map[variant] = canonical
            normalized = _concept_key(variant)
            if normalized:
                alias_map[normalized] = canonical
    return alias_map
