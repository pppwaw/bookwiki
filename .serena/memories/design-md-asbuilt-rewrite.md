# design.md LangGraph→as-built rewrite (ground truth)

Task: User picked Option A — fully rewrite design.md's fictional LangGraph framing to match
the as-built custom `BookGraph`. ~92 grep sites. design.md = /root/pppwawProjects/bookwiki/design.md (2023 lines).
Already done earlier: instruction docs (AGENTS.md/SKILL.md/runbook.md/contracts.md), §17.4 table,
`--force-from`→`--from … --force`, deleted plan.md + docs/superpowers/.

## VERIFIED as-built facts (re-read from code this session)

scheduler/graph.py:
- custom `BookGraph` dataclass + `GraphView.draw_mermaid()` (hand-rolled "graph TD" string). NO langgraph import.
- `NODE_ORDER` = 11 nodes: convert, caption, structure, split, generate, reconcile_concepts,
  concept_pages, integrate, check, repair, index. (design.md wrongly says "10 个 node")
- checkpoint = JSON at `cfg.cache_dir/"checkpoint.json"` = `work/.cache/checkpoint.json`
  payload {status(running|paused|completed), next_node, config_hash, state}. NOT SqliteSaver/langgraph.sqlite.
- manifest = `work/logs/run-manifest.json` (book_id,status,next_node,config_hash,nodes[],outputs).
- `interrupt_before: list[str] = ["split"]` IS a real BookGraph field (KEEP this concept).
- invoke(): manual `while index < len(NODE_ORDER)` loop; pause_after list + stop_after honored in loop.
- `_run_node`: `result = fn(state, cfg)`; if awaitable → `asyncio.run(result)`.
- `_config_hash()` sha256[:16] of cfg.to_json()+book_notes_hash → on mismatch reruns from convert/caption.
- `resume_or_start(graph, book_id, resume)` → `graph.invoke({"book_id":book_id}, resume=resume)`. NOT invoke(None).
- force: `cfg.force_from` → `_clear_for_force` (unlink checkpoint.json + rmtree cache_dir/tasks) + `_state_for_force_from`.
- dry_run → `{"dry_run":True,"report": draw_mermaid()+summarize()}`.
- repair loop: check→repair→integrate→check via state["repair_targets"].

scheduler/cache.py — `run_with_cache(agent_cls,*inputs,model,cache_dir="work/.cache/tasks",force,runtime)`:
- per-task JSON at `cache_dir/tasks/<key>.json`. `task_key` = `{kind}-{sha256[:24]}` of payload
  {agent=module.name, kind, model, prompt=prompt_cache_key(prompt_template), inputs=_jsonable}. force bypasses.
- NOT diskcache, NOT "input_hash", NOT cache.db. NO automatic file-watch cascade.

agents/prompting.py — `prompt_cache_key` = sha256[:16] of COMMON_SYSTEM_PROMPT+USER_PROMPT_TEMPLATE+agent.body.
  So cache invalidation = hash(actual prompt template content)+model+serialized inputs. NO prompt_version/schema_version fields.

scheduler/dry_run.py — static `ESTIMATE` dict (10 kinds) + `size/4` heuristic; `summarize(nodes,chapter_count)`. NOT tiktoken.

scheduler/budget_guard.py — `enforce_budget(router,max_cost_usd)` reads router.usage_logs via total_cost_usd, raises BudgetExceeded (~25 lines).
  **CRITICAL: enforce_budget is NEVER called anywhere — defined only, not wired in.** budget default {maxCostUsd: 2.0}.

scheduler/llm.py — REAL litellm+instructor (KEEP):
- `LLMRuntime` Protocol .generate(); `LiteLLMRuntime` → build_router()=`from litellm import Router`
  (routing_strategy="usage-based-routing-v2", num_retries=3, retry_after=2, fallbacks deepseek-v4-pro→deepseek-v4-flash)
  + build_instructor_client=`instructor.from_litellm(router.acompletion, mode=instructor.Mode.JSON)`.
  client.create(model,response_model,messages,max_retries,temperature).
- `TestLLMRuntime` for BOOKWIKI_TEST_LLM=1. build_runtime() picks by env.
- model_list: deepseek-v4-pro(tpm200k/rpm60), deepseek-v4-flash(tpm400k/rpm120), kimi-k2.6(moonshot api_base moonshot.cn). Keys DEEPSEEK_API_KEY/MOONSHOT_API_KEY. NO gemma-4/vertex_ai.

scheduler/config.py paths: work_dir=book_dir/work, cache_dir=work_dir/.cache, content_dir=book_dir/content/docs,
  site_dir=book_dir/site, input_dir=book_dir/input. force_from/pause_after/dry_run are BookConfig fields.
  DEFAULT_MODELS: structure/lesson/chapter/concept/review=deepseek-v4-pro; source_summary/split/summary/card/source_layout_repair=deepseek-v4-flash; vision=kimi-k2.6. budget default maxCostUsd 2.0.

pipeline/nodes.py fan-out: `asyncio.Semaphore(max_concurrent)` + `asyncio.gather(*(run_group(g)...))`, max_concurrent default 10. NOT Send.

pyproject.toml [optional-dependencies] runtime declares langgraph/litellm/instructor/diskcache/tiktoken —
  only litellm+instructor actually imported. (out of scope: user asked design.md only; do not edit pyproject without asking.)

## Terminology map (FICTIONAL → AS-BUILT)
- "LangGraph 顶层图 / StateGraph" → 自研 `BookGraph` 顶层流水线 (scheduler/graph.py)
- "SqliteSaver / langgraph.sqlite" → JSON checkpoint `work/.cache/checkpoint.json`
- "Send API fan-out" → `asyncio.Semaphore` + `asyncio.gather`
- "interrupt_before=['split']" → KEEP (real field)
- "interrupt_after=pause_after / --pause-after" → pause_after 列表 + 循环判断 (real behavior, not langgraph)
- "graph.invoke(None,config) 续跑" → resume_or_start → invoke({'book_id':..},resume=True),靠 checkpoint.json next_node
- "diskcache / input_hash / cache.db / results/" → 自研 per-task JSON 缓存 `work/.cache/tasks/<key>.json` (run_with_cache)
- "input_hash 级联失效" → task_key 内容哈希命中/未命中;无自动文件监听级联;失效靠输入内容传播
- "tiktoken 估算" → 静态 ESTIMATE 表 + size/4 (dry_run.py)
- "LangSmith trace" → 结构化日志 (get_logger) + run-manifest.json
- "budget_guard 每次调用前强制" → enforce_budget helper 存在但未接线;仅 budget.maxCostUsd 字段(默认2.0)
- "10 个 node" → 11 个 node
- KEEP REAL: litellm Router(限速/重试/回退/成本)、instructor(JSON 结构化)、node DAG 名、双层(checkpoint 决定 node 是否重跑 + task 缓存决定 LLM 是否真调)、split 人工闸门、asyncio 并发。

## Progress — COMPLETE
- ALL sections rewritten: §1/§3/§4/§7/§9/§10/§11/§13/§17(.1-.7)/§18(.3/.5/.6/.9)/§19/§21.1/§22/§23/§24/§25/§27.
- Final grep verify PASSED: only 7 residual hits, ALL legitimate — 6 explicit negations (L796/863/1012/1014/1602/1684 "没有 LangGraph/不是 Send/没有 LangSmith/不调 tiktoken") + 1 real `_schema_version` field (L1890). Zero hits for StateGraph/SqliteSaver/Send(/interrupt_after/diskcache/cache.db/langgraph.sqlite/input_hash/thread_id/get_state/prompt_version/.router/checkpointer.
- Substantive fix shipped: `--force` wipes whole per-task cache (`_clear_for_force` rmtree tasks/) → false "未受影响章节命中缓存" claim corrected. `generate.py` stops at `generate` node (not integrate). agent_results has `_schema_version` only.
- pyproject.toml unused deps (langgraph/diskcache/tiktoken) left untouched per scope (design.md only).

## Remaining edit zones in design.md (line refs approx, re-grep before editing)
- L4-5 头部 blockquote; L59 mermaid label; L81 §3 调度模块行; L135-146 §4 dir tree (.cache/langgraph.sqlite/results/cache.db + run-manifest LangSmith note)
- L557,583-605 §7; L764,788 §9; **§10 L795-1066 (core)**; L1069 §11 integrate; L1141,1211 §12/13
- §17 L1383-1644 (17.1 L1397/17.4 table DONE earlier/17.5 L1496-1507 node names + "10 个"/17.6 L1508-1520 resume_or_start/17.7 L1531-1544 two-layer table)
- L1596,1604,1644 dry-run/generate; L1674-1719 §18 dir tree + test names; L1751,1814,1830 §19; L1886-1985 §20-26 (work/.cache tree, acceptance, member matrix, demo)

## Verify after: grep design.md for langgraph|LangGraph|StateGraph|SqliteSaver|Send\(|interrupt_after|diskcache|tiktoken|LangSmith|cache\.db|langgraph\.sqlite → expect ~0. interrupt_before=['split'] may stay.
