# MinerU Setup

BookWiki M2 requires MinerU for PDF/PPTX parsing. BookWiki supports two production backends:

- `local`: a self-hosted `mineru-api` service.
- `cloud-v4`: MinerU Open API v4 with signed upload and token auth.

If the selected backend is unavailable, conversion fails fast instead of falling back to another
backend.

## Local Services

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

For the self-hosted API:

```bash
MINERU_BACKEND=local
MINERU_API_URL=http://127.0.0.1:8000
MINERU_API_TIMEOUT_SECONDS=20
MINERU_API_POLL_INTERVAL_SECONDS=2
```

For MinerU cloud v4:

```bash
MINERU_BACKEND=cloud-v4
MINERU_API_TOKEN=your-mineru-token
MINERU_MODEL_VERSION=vlm
MINERU_API_TIMEOUT_SECONDS=300
MINERU_API_POLL_INTERVAL_SECONDS=2
```

The cloud endpoint defaults to `https://mineru.net`. Set `MINERU_CLOUD_API_URL` only if you need
to route through a mirror, proxy, or staging endpoint. `MINERU_API_URL` is reserved for the local
backend and is ignored by `cloud-v4`.

`MINERU_API_TOKEN` may also be provided as `MINERU_TOKEN` for compatibility with MinerU's
official CLI/SDK naming. `MINERU_MODEL_VERSION` defaults to `vlm`.

## Conversion Behavior

With `MINERU_BACKEND=local`, `bookwiki.convert.mineru_client.convert_pdf_to_md` uses this order:

1. `GET /health` on `MINERU_API_URL`.
2. `POST /tasks` with `return_md=true`.
3. Poll `GET /tasks/{task_id}` until the task reaches a completed or failed status.
4. Fetch `GET /tasks/{task_id}/result` and read returned Markdown.
5. If the health check, task submission, polling, or result fetch fails, raise
   `MineruConversionError` and stop the run.

With `MINERU_BACKEND=cloud-v4`, the client uses MinerU Open API v4:

1. Require `MINERU_API_TOKEN`.
2. `POST /api/v4/file-urls/batch` with `model_version=vlm` and the local filename.
3. `PUT` the source file to the returned signed upload URL.
4. Poll `GET /api/v4/extract-results/batch/{batch_id}` until the file reaches `done` or
   `failed`.
5. Download `full_zip_url`, read `full.md`, content-list JSON, and image assets from the zip.
6. If token validation, upload, polling, zip download, or result extraction fails, raise
   `MineruConversionError` and stop the run.

There is intentionally no `vlm-http-client`, local `pipeline`, Agent lightweight API, or metadata
fallback for PDF/PPTX input.
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
