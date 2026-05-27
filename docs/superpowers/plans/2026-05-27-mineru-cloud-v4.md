# MinerU Cloud V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a MinerU cloud-v4 backend while preserving the existing local `mineru-api` backend.

**Architecture:** Keep `bookwiki.convert.mineru_client.convert_document_to_source()` as the pipeline boundary. Dispatch by `MINERU_BACKEND=local|cloud-v4`; local keeps `/health` + `/tasks`, while cloud-v4 performs signed upload, batch polling, zip download, and then reuses the existing zip extraction/normalization path.

**Tech Stack:** Python stdlib `urllib`, existing pytest HTTP fixtures, BookWiki conversion pipeline.

---

### Task 1: Cloud V4 Client

**Files:**
- Modify: `tests/test_convert_m2.py`
- Modify: `bookwiki/convert/mineru_client.py`
- Modify: `.env.example`
- Modify: `docs/mineru-setup.md`

- [ ] **Step 1: Write failing tests**

Add tests for `MINERU_BACKEND=cloud-v4` that verify:
- `MINERU_API_TOKEN` is required.
- The client calls `/api/v4/file-urls/batch`, uploads bytes to the returned signed URL, polls `/api/v4/extract-results/batch/{batch_id}`, downloads `full_zip_url`, and extracts Markdown/assets from the zip.
- `convert_node` still writes `sources_md` and source-ref manifests when cloud-v4 is selected.

Run: `pytest tests/test_convert_m2.py::test_convert_document_to_md_uses_mineru_cloud_v4_upload_flow tests/test_convert_m2.py::test_cloud_v4_requires_mineru_api_token tests/test_convert_m2.py::test_convert_node_uses_mineru_cloud_v4_backend -q`

Expected: FAIL because the backend selector and cloud-v4 API flow do not exist.

- [ ] **Step 2: Implement backend selector and cloud-v4 flow**

Add:
- `MINERU_BACKEND` selector with default `local`.
- `MINERU_API_TOKEN` validation for `cloud-v4`.
- `MINERU_MODEL_VERSION` defaulting to `vlm`.
- Signed-upload request, PUT upload, batch polling, zip download, and reuse of `_extract_source_from_zip()`.

- [ ] **Step 3: Run focused tests**

Run: `pytest tests/test_convert_m2.py::test_convert_document_to_md_uses_mineru_cloud_v4_upload_flow tests/test_convert_m2.py::test_cloud_v4_requires_mineru_api_token tests/test_convert_m2.py::test_convert_node_uses_mineru_cloud_v4_backend -q`

Expected: PASS.

- [ ] **Step 4: Run conversion test file**

Run: `pytest tests/test_convert_m2.py -q`

Expected: PASS.

- [ ] **Step 5: Update docs/env**

Document:
- `MINERU_BACKEND=local|cloud-v4`
- `MINERU_API_TOKEN`
- `MINERU_API_URL` for local
- `MINERU_CLOUD_API_URL` as the optional cloud override; cloud-v4 defaults to `https://mineru.net`
- fail-fast behavior and no Agent lightweight fallback.

- [ ] **Step 6: Final verification**

Run: `pytest tests/test_convert_m2.py -q`

Expected: PASS.
