# MinerU Setup

BookWiki M2 uses a local MinerU API first, then falls back to local MinerU pipeline parsing when
the API is unavailable or a request times out.

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
MINERU_VLM_URL=http://127.0.0.1:30000
MINERU_API_TIMEOUT_SECONDS=20
```

For deterministic offline tests or CPU-only fallback runs:

```bash
MINERU_API_DISABLED=1
```

## Conversion Behavior

`bookwiki.convert.mineru_client.convert_pdf_to_md` uses this order:

1. `GET /health` on `MINERU_API_URL`.
2. `POST /file_parse` with `return_md=true` and `response_format_zip=false`.
3. If API parsing fails but the service is healthy, try MinerU `do_parse` with
   `backend="vlm-http-client"`.
4. If the API is down or VLM client parsing fails, use MinerU `do_parse` with
   `backend="pipeline"`.
5. If MinerU is not installed in the current Python environment, emit metadata Markdown instead
   of failing the whole pipeline.

All PDF Markdown is normalized to include page source refs like:

```markdown
<!-- source_ref: textbook-p001 -->
```

PPTX and text inputs use:

```markdown
<!-- source_ref: lecture9-slide01 -->
<!-- source_ref: notes-text -->
```
