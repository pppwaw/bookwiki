# 设计:统一中文搜索后端 + 轻量语义混合检索(hybrid)

- 日期:2026-06-30
- 分支:`feat/search-fts`
- 状态:设计已与用户确认,待 spec 评审 → 进入实现计划

## 1. 背景与问题

项目当前有**两条完全独立**的搜索链路,二者对中文都基本失效:

1. **fumadocs 文档搜索框(Orama)** —— `site-template/app/api/search/route.ts`,仅 `createFromSource(source, { language: 'english' })`。`RootProvider`(`app/layout.tsx`)未传 `search` 配置,故默认搜索弹窗(Ctrl+K)是开启的,走 `fetchClient` 打 `GET /api/search?query=`。当前英文 stemmer,无 CJK 分词。
2. **RAG 聊天检索(SQLite FTS5)** —— `site-template/lib/rag.ts` 的 `searchChunks()`,被 `/api/chat` 的 `search_book` 工具调用。索引由 Python `bookwiki/indexer/sqlite_builder.py` 构建,FTS5 `tokenize='unicode61 remove_diacritics 2'`。纯关键词,无向量、无 rerank,FTS 失败回退 `LIKE '%整串%'`。

### 已实测的根因

`unicode61` 把**整段中文当成一个 token**(原文「反向传播是一种用于训练神经网络的算法」):查 `反向传播`→0、查 `神经网络`→0,只有一字不差的整段才命中。叠加 `toFtsQuery` 按空格切词、中文无空格,几乎永远查不到,回退 `LIKE` 同样无效。**结论:当前中文站内搜索(文档框与 RAG)都坏,且均为纯关键词、无语义。**

## 2. 目标与非目标

### 目标
- 统一为**一套** Chinese-aware 的 SQLite 检索后端,同时服务「文档搜索框」与「RAG 聊天」。
- 修好中文关键词检索。
- 叠加**轻量语义检索**(同义/换种说法可召回),不引入向量数据库、不引入 SQLite 原生扩展、不在 Vercel 函数里打包模型。
- 关键词与语义两路用 **RRF** 融合。

### 非目标
- 不引入 sqlite-vec / faiss / qdrant / pgvector 等向量库或原生扩展。
- 不在客户端或 Vercel 函数内运行 embedding 模型(不打包 onnxruntime / 不下载模型到浏览器)。
- 不做 cross-encoder rerank(RRF 已够用,留作未来)。

## 3. 架构总览

一个 SQLite 文件,**两层召回**(trigram 关键词 + 向量语义)、**两个入口**(文档框 + 聊天)、**RRF 融合**。向量由 **OpenRouter embeddings** 提供:passage 在构建期算好存进 SQLite,query 在运行时(服务端)算。

```
构建期(Python)  MDX → chunk → trigram FTS  +  OpenRouter embed → embedding BLOB ──┐
                                                                                   ├─► bookwiki.sqlite
运行期(Node/Vercel) query ─► OpenRouter embed ─► [向量 cosine 排名] ┐             │
                            └────────────────────► [trigram BM25 排名]┴─ RRF ─► top-k ┘
                                  ▲ 文档框 GET /api/search / chat search_book 共用同一函数
```

规模假设:单本书 chunk 量级数百~数千,1024 维 float32 全库暴力 cosine 在 JS 中 < 5ms,无需近邻索引。

## 4. Embedding 方案(OpenRouter 全程)

- **模型**:`baai/bge-m3`(OpenRouter,1024 维,多语/中文强)。passage 与 query **两侧同一模型** → 向量天然可比、一致性无需额外保证。
- **可切换**:模型名与维度写入环境变量(如 `BOOKWIKI_EMBED_MODEL`、`BOOKWIKI_EMBED_DIM`)+ `search_meta` 表;切到 `qwen/qwen3-embedding-4b` 等只改配置并重建索引。
- **前缀**:bge-m3 query 与 passage 均**不加**指令前缀(与 bge-zh-v1.5 不同),两侧都按原文送。
- **归一化**:两侧 L2 normalize,cosine 退化为点积。
- **凭据**:复用现有 OpenRouter 凭据。构建期用 `OPENROUTER_API_KEY`(pipeline 已有 LLM key 体系);运行期用站点现有 `BOOKWIKI_CHAT_API_KEY` / `BOOKWIKI_CHAT_BASE_URL`(默认 openrouter)。缺失即报错,不做 mock/fallback。
- **成本**:整本书 passage 一次性约几毫美元;每次 query 可忽略。

## 5. 详细设计

### 5.1 数据库 schema 变更 — `bookwiki/indexer/sqlite_builder.py`

`_create_schema`:
1. FTS5 分词器改 trigram:`tokenize='trigram'`(替换 `unicode61 remove_diacritics 2`)。trigram = 连续 3 字符滑窗的子串索引;≥3 字子串可 MATCH,<3 字走 LIKE(trigram 索引加速)。
2. `chunks` 新增 `embedding BLOB`(float32 little-endian,1024 维 = 4096 字节)。
3. 新增 `search_meta` 表:`embedding_model`、`embedding_dim`,运行时校验不匹配即报错。

### 5.2 构建期 embedding — `bookwiki/indexer/`

- 新增 `embedder.py`:调 OpenRouter `/api/v1/embeddings`(OpenAI 兼容)批量对 chunk passage 文本算向量、L2 normalize、打包成 float32 bytes。优先尝试现有 `litellm`(已是依赖)的 `embedding()`;若其 OpenRouter embedding 支持不稳,退为直接 HTTP POST(与 TS 侧 `embedding.ts` 对称)。实现阶段先验证 litellm 路径再定。
- `sqlite_builder._insert_chunks`:写入 `embedding` 列;写 `search_meta`。
- 失败(无 key / 网络 / 维度不符)直接抛错,不静默跳过。
- **无新增重依赖**(复用 litellm;不引 fastembed/torch)。

### 5.3 运行期共享检索 — `site-template/lib/rag.ts`

`searchChunks(query, limit, chapterId)` **签名不变**,内部升级为 hybrid:
1. **关键词路**:trigram FTS。重写 `toFtsQuery`:term 长度 ≥3 → trigram 短语 `"term"` 以 `OR` 连接;<3 → 该路走现有 `LIKE` 回退。保留 `try(FTS) catch(LIKE)`。取 top-N(N≈30)+ bm25 排名。
2. **语义路**:`embedQuery(query)`(见 5.6)调 OpenRouter 算 query 向量 → **扫全库**(有 `chapterId` 时为该章子集)`embedding` BLOB → 暴力 cosine(已 normalize → 点积)→ top-N。语义路**不能**只在关键词候选里算,否则语义召回被关键词限制,失去「按意思搜」意义。
3. **融合**:对两路**排名**做 RRF:`score(d)=Σ_path 1/(k+rank_path(d))`,`k=60`;按融合分取最终 `limit`。
4. 返回 `SearchChunk[]` 不变 → `search_book` 与文档 API 共用。
5. **降级语义**:若 `embedQuery` 失败(OpenRouter 不可用),记录告警并仅返回 trigram 关键词结果(仍是真实词法结果,非伪造数据,符合 no-mock);不静默假装语义已生效。**此行为待用户在评审中确认。**

新增辅助模块(保持 `rag.ts` 聚焦):
- `lib/embedding.ts`:服务端 `embedQuery(text): Promise<Float32Array>`,POST OpenRouter `/api/v1/embeddings`(OpenAI 兼容),复用 `BOOKWIKI_CHAT_*` 配置。
- `lib/vector.ts`:BLOB ↔ Float32Array、点积/cosine、top-N。
- `lib/fusion.ts`:RRF。

### 5.4 文档搜索 API — `site-template/app/api/search/route.ts`(整段重写)

- 移除 `createFromSource`。
- 自定义 `GET(request)`:读 `?query=` → 调 `searchChunks`(hybrid)→ 映射 fumadocs `SortedResult[]`:命中 page → `{type:'page',content:title,url}`;其下命中 chunk → `{type:'text',content:片段,url:/docs/<slug>#<anchor>}`;`id` 用 chunk_id / slug 稳定唯一。
- 前端 `RootProvider` 与默认搜索弹窗不改;fumadocs fetch 客户端**默认带 debounce**,叠加每次查询一次 OpenRouter 调用成本可接受;如需更省,可让 trigram 即时返回、语义在停顿后补(本期先用默认 debounce,简单优先)。

### 5.5 聊天检索 — `site-template/app/api/chat/route.ts`

- **保持 agentic 不变**:`search_book` 仍由 LLM 决定 query。因为服务端现在能廉价 embed 任意 query,`searchChunks` 升级后聊天**自动获得语义**,无需改动工具协议。

### 5.6 SQLite 连接与 embedding 单例(小优化,纳入本次)

- `lib/sqlite.ts`:当前每查询开/关只读连接;改为模块级缓存只读连接(进程内复用),因为搜索框高频且每次要读向量 BLOB。
- `lib/embedding.ts`:复用一个 fetch 客户端;无模型常驻,Vercel 函数体积不受影响。

### 5.7 构建顺序

embedding 在 Python `build_sqlite_index`(`index_node` 调用)内完成,产物仍是 `cfg.site_dir/.bookwiki/bookwiki.sqlite`,site 端 `next build` 不变。索引逻辑保持单一来源(Python)。构建期需 `OPENROUTER_API_KEY`,与现有 pipeline LLM 调用一致。

## 6. 融合算法:RRF

选 RRF 而非加权求和:对两路分数尺度(bm25 rank vs cosine)不敏感、无需调权重、稳健。`rrf(d)=Σ_{path∈{kw,vec}} 1/(k+rank_path(d))`,`k=60`;仅出现在单路的文档也计入(缺席路不贡献)。未来需偏置再加权重。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 语义路运行时依赖 OpenRouter 可用性 | 关键词 trigram 本地即时;embedding 失败降级为关键词结果 + 告警(见 5.3,待确认) |
| trigram 对 <3 字中文词(如「算法」)MATCH 无效 | 该路落 LIKE(trigram 索引加速);语义路兜召回 |
| 文档框每次查询一次 embedding 调用成本 | 模型极廉 + fumadocs 默认 debounce;必要时语义延后到停顿触发 |
| 向量 BLOB 增大 DB(~4KB/chunk) | 单本书几 MB,可接受 |
| 模型切换导致旧向量失配 | `search_meta` 记录 model/dim,运行时校验不符即报错并提示重建 |
| 构建期需联网与 key | 与现有 pipeline LLM 依赖一致;缺失即报错 |

## 8. 测试计划

- **Python**
  - trigram 建索引后断言「反向传播」「神经网络」能命中(最小复现的反向断言)。
  - embedding 写入:每 chunk 1024 维向量、`search_meta` 正确;无 key 时报错路径。
- **TypeScript**
  - `toFtsQuery`:≥3 字走 FTS、<3 字走 LIKE 分支。
  - `vector.ts`:BLOB 往返、cosine 正确性、top-N。
  - `fusion.ts`:已知排名 → 期望融合次序。
  - 语义召回端到端(可对 `embedQuery` 打桩固定向量):只写「反向传播」的 chunk,query「梯度回传」能召回。
  - `/api/search`:返回 `SortedResult` 形状 + 中文 query 命中。
  - 降级路径:`embedQuery` 抛错时仅返回关键词结果且不崩。

## 9. 实施阶段(交由 writing-plans 细化)

1. 构建期:trigram + schema(`embedding`、`search_meta`)+ `embedder.py`(litellm→OpenRouter)+ 写入 + 单测。
2. 运行期工具层:`embedding.ts` / `vector.ts` / `fusion.ts` + 单测。
3. `rag.ts` 升级 hybrid + `toFtsQuery` 重写 + 降级逻辑 + 单测(聊天此时即获得语义)。
4. `/api/search` 重写为自定义中文后端 + `sqlite.ts` 连接缓存 + 端到端测试。
5. 配置项(`BOOKWIKI_EMBED_MODEL`/`_DIM`)、文档与部署说明(Vercel:无模型打包;构建需 `OPENROUTER_API_KEY`)。

## 10. 涉及文件清单

- `bookwiki/indexer/sqlite_builder.py`(schema、trigram、embedding 写入、search_meta)
- `bookwiki/indexer/embedder.py`(新增,litellm→OpenRouter)
- `site-template/lib/rag.ts`(hybrid、toFtsQuery、降级)
- `site-template/lib/embedding.ts`(新增,服务端 query embedding)
- `site-template/lib/vector.ts`(新增)
- `site-template/lib/fusion.ts`(新增)
- `site-template/lib/sqlite.ts`(连接缓存)
- `site-template/app/api/search/route.ts`(重写)
- `pyproject.toml`(无需新增重依赖,复用 litellm;如需独立 HTTP 客户端再议)
