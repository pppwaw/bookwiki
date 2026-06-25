# BookWiki Artifact Contracts

## Source Ref Manifests

Path (one per source, written by `convert`, mutated in place by `caption`):

```text
books/<id>/work/source_refs/<source_id>.json
```

Shape:

```json
{
  "source_id": "ai-intro",
  "ref_granularity": "page",
  "pages": [
    {
      "page_idx": 0,
      "page_number": 1,
      "source_ref": "ai-intro-p001",
      "blocks": [
        {
          "block_id": "ai-intro-p001-b001",
          "type": "image",
          "asset_path": "work/assets/ai-intro/asset-001.png",
          "caption": "Figure 1: search tree",
          "caption_source": "vision",
          "caption_model": "kimi-k2.6"
        }
      ]
    }
  ],
  "vision_warnings": []
}
```

`source_ref` values (e.g. `ai-intro-p001`) are the citation anchors that approved structure, agents, and MDX reference. The `caption` stage fills `caption`/`caption_source`/`caption_model` on image and chart blocks that have an `asset_path`, appends `vision_warnings`, and fails loudly if any image caption errors.

## Approved Structure

Path:

```text
books/<id>/work/structure/approved-structure.yaml
```

Required marker:

```text
# bookwiki: approved-structure
```

Expected shape:

```yaml
chapters:
  - title: Chapter 1 Search
    topics:
      - State space search
    source_refs:
      - source-p001
```

The marker is a hard human-review gate. The scheduler must not split chapters without it.

## Agent Results

Path:

```text
books/<id>/work/agent_results/
```

Chapter-level files (one set per chapter id):

```text
<chapter-id>.chapter.json     # written tagged _agent=SectionAgent (assembled section pipeline body)
<chapter-id>.summary.json     # _agent=SummaryAgent
<chapter-id>.quiz.json        # _agent=ApplicationQuizAgent (knowledge + application items merged)
<chapter-id>.card.json        # _agent=CardAgent
<chapter-id>.concepts.json    # per-chapter concept candidates
```

Wrapper shape (written by `_agent_result_payload` in `bookwiki/pipeline/nodes.py`):

```json
{
  "_schema_version": "llm.v1",
  "_agent": "SectionAgent",
  "_model": "deepseek-v4-pro",
  "result": {}
}
```

`_agent` is the producing agent class name (e.g. `SectionAgent`, `SummaryAgent`, `ApplicationQuizAgent`, `CardAgent`, `SkeletonAgent`), not a single legacy "lesson" agent. Agents return Pydantic models. Agents do not write final Markdown; scheduler nodes write JSON and the integrator renders MDX.

## Book Skeleton

Path (written by `build_skeleton`, read-only downstream):

```text
books/<id>/work/skeleton.json
```

`SkeletonAgent` runs once over every chapter source to produce the book-wide contract: a canonical glossary with each concept's first-owning chapter, an `alias_map` (every variant -> canonical), and one-line `chapter_briefs`. `generate` injects each chapter's slice so chapters share terminology and can write neighbour transitions. It uses the same wrapper shape as agent results (`_agent=SkeletonAgent`).

## Reconciled Concepts

Path (canonical, written by `reconcile_concepts`):

```text
books/<id>/work/concepts/reconciled.json
```

The same payload is mirrored to `books/<id>/work/agent_results/concepts.reconciled.json`, and the alias map is also written standalone to `books/<id>/work/concepts/alias_map.json`.

Shape:

```json
{
  "schema_version": "llm.v1",
  "concepts": [
    {
      "schema_version": "llm.v1",
      "canonical": "state space",
      "aliases": ["states"],
      "source_chapter_ids": ["chapter-1"]
    }
  ],
  "alias_map": {
    "states": "state space"
  }
}
```

The integrator uses `alias_map` to normalize `[[alias]]` links and uses concept page output paths for final MDX links.

## Check Report

Paths:

```text
books/<id>/work/logs/check-report.json
books/<id>/work/logs/check-report.md
books/<id>/work/check-report.json
```

JSON shape:

```json
{
  "schema_version": "llm.v1",
  "status": "needs_repair",
  "issues": [
    {
      "schema_version": "llm.v1",
      "severity": "error",
      "code": "BROKEN_LINK",
      "message": "chapter-1.mdx links to missing target",
      "owner_task_id": "chapter-1:chapter"
    }
  ],
  "repair_targets": ["chapter-1:chapter"]
}
```

Only `error` and `critical` issues become `repair_targets`.

## Run Manifest

Path:

```text
books/<id>/work/logs/run-manifest.json
```

Shape:

```json
{
  "book_id": "mini",
  "status": "paused",
  "next_node": "split",
  "config_hash": "abc123",
  "nodes": [
    {"name": "convert", "status": "completed", "cache_hit": false}
  ],
  "llm_usage": {
    "currency": "CNY",
    "total_cost_cny": 1.234568,
    "prompt_tokens": 120,
    "completion_tokens": 34,
    "total_tokens": 154,
    "budget_max_cost_cny": 70.0,
    "stages": [
      {
        "name": "convert",
        "currency": "CNY",
        "cost_cny": 0.25,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120
      }
    ]
  },
  "outputs": {
    "content": "books/mini/content/docs",
    "sqlite": "site/.bookwiki/bookwiki.sqlite"
  }
}
```

Use this file to decide whether to resume, approve structure, inspect a failed stage, or check the actual LLM spend after a run.

## SQLite

Path:

```text
books/<id>/site/.bookwiki/bookwiki.sqlite
```

Core tables and views:

- `documents`: MDX pages with path, title, type, slug, frontmatter, and body text.
- `chunks`: RAG chunks tied to document paths and headings.
- `learning_items`: quiz and Anki items parsed from MDX components.
- `source_refs`: citation refs and quotes parsed from MDX.
- FTS/search objects are rebuilt from the generated content on each index run.

Rebuild with:

```bash
python scripts/run.py books/<id> --to index
```
