# BookWiki Artifact Contracts

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

Chapter-level files:

```text
<chapter-id>.chapter.json
<chapter-id>.summary.json
<chapter-id>.quiz.json
<chapter-id>.card.json
<chapter-id>.concepts.json
```

Wrapper shape:

```json
{
  "_schema_version": "llm.v1",
  "_agent": "LessonAgent",
  "_model": "deepseek-v4-pro",
  "result": {}
}
```

Agents return Pydantic models. Agents do not write final Markdown; scheduler nodes write JSON and the integrator renders MDX.

## Reconciled Concepts

Path:

```text
books/<id>/work/concepts/reconciled.json
```

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
  "outputs": {
    "content": "books/mini/content/docs",
    "sqlite": "site/.bookwiki/bookwiki.sqlite"
  }
}
```

Use this file to decide whether to resume, approve structure, or inspect a failed stage.

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
python scripts/index.py books/<id>
```
