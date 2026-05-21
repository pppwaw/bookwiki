# MinerU Setup

BookWiki M2 requires a local MinerU API for PDF parsing. If the API is unavailable,
PDF conversion fails fast instead of falling back to another backend.

## Services

Install the runtime on the machine that will run parsing:

```bash
uv pip install "mineru[pipeline,core]"
```

On the GPU host, start the OpenAI-compatible VLM server:

```bash
mineru-openai-server --engine vllm --port 30000
```

Start the MinerU API service:

```bash
mineru-api --host 0.0.0.0 --port 8000 --enable-vlm-preload true
```

The default local endpoint is:

```text
http://127.0.0.1:8000
```

## Health Check

```bash
curl http://127.0.0.1:8000/health
```

A healthy service returns JSON with `status`, `version`, `protocol_version`,
`processing_window_size`, and queue counters. The current local smoke check was run against
MinerU API `3.1.3`.

## BookWiki Environment

```bash
MINERU_API_URL=http://127.0.0.1:8000
MINERU_API_TIMEOUT_SECONDS=20
MINERU_API_POLL_INTERVAL_SECONDS=2
```

## Conversion Behavior

`bookwiki.convert.mineru_client.convert_pdf_to_md` uses this order:

1. `GET /health` on `MINERU_API_URL`.
2. `POST /tasks` with `return_md=true`.
3. Poll `GET /tasks/{task_id}` until the task reaches a completed or failed status.
4. Fetch `GET /tasks/{task_id}/result` and read returned Markdown.
5. If the health check, task submission, polling, or result fetch fails, raise
   `MineruConversionError` and stop the run.

There is intentionally no `vlm-http-client`, `pipeline`, or metadata fallback for PDF input.
Offline tests should use TXT/PPTX fixtures unless they explicitly start `mineru-api`.

All PDF Markdown is normalized to include page source refs like:

```markdown
<!-- source_ref: textbook-p001 -->
```

PPTX and text inputs use:

```markdown
<!-- source_ref: lecture9-slide01 -->
<!-- source_ref: notes-text -->
```
