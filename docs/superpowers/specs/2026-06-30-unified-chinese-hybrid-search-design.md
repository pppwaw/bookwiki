# 设计:统一中文搜索后端 + 轻量语义混合检索(hybrid)

- 日期:2026-06-30
- 分支:`feat/search-fts`
- 状态:设计已与用户确认,待 spec 评审 → 进入实现计划

## 1. 背景与问题

项目当前有**两条完全独立**的搜索链路,二者对中文都基本失效:

1. **fumadocs 文档搜索框(Orama)** —— `site-template/app/api/search/route.ts`,仅 `createFromSource(source, { language: 'english' })`。`RootProvider`(`app/layout.tsx`)未传 `search` 配置,故默认搜索弹窗(Ctrl+K)是开启的,它走 `fetchClient` 打 `GET /api/search?query=`。当前用英文 stemmer,无 CJK 分词。
2. **RAG 聊天检索(SQLite FTS5)** —— `site-template/lib/rag.ts` 的 `searchChunks()`,被 `/api/chat` 的 `search_book` 工具调用。索引由 Python `bookwiki/indexer/sqlite_builder.py` 构建,FTS5 虚拟表 `tokenize='unicode61 remove_diacritics 2'`。纯关键词,无向量、无 rerank,FTS 失败回退 `LIKE '%整串%'`。

### 已实测的根因

`unicode61` 把**整段中文当成一个 token**。最小复现(原文「反向传播是一种用于训练神经网络的算法」):

| 查询 | 命中 |
|---|---|
| `"反向传播"` | 0 |
| `"神经网络"` | 0 |
| 完整整段 | 1(一字不差才命中) |
| 英文 `"neural"` | 1(英文正常) |

再叠加 `toFtsQuery` 按空格切词(`query.split(/\s+/)`),中文无空格 → 整句成一个词 → 几乎查不到 → 回退 `LIKE '%整句%'` 同样要求整句是子串 → 仍查不到。**结论:当前中文站内搜索(文档框与 RAG)都是坏的,且二者均为纯关键词、无语义。**

## 2. 目标与非目标

### 目标
- 统一为**一套** Chinese-aware 的 SQLite 检索后端,同时服务「文档搜索框」与「RAG 聊天」。
- 中文关键词检索可用(修 bug)。
- 叠加**轻量语义检索**,做到「按意思搜」(同义/换种说法也能召回),且不引入向量数据库、不引入 SQLite 原生扩展、不依赖在线 API。
- 关键词与语义两路用 **RRF** 融合。

### 非目标
- 不引入 sqlite-vec / faiss / qdrant / pgvector 等向量库或原生扩展。
- 不引入在线 embedding API(no-fallback 政策下也不做 API 兜底)。
- 不做独立的重排序模型(cross-encoder rerank);RRF 已够用,留作未来。

## 3. 架构总览

一个 SQLite 文件,**两层召回**(trigram 关键词 + 向量语义)、**两个入口**(文档框 + 聊天)、**RRF 融合**。重活在构建期,运行时只多一次 query 向量 + 内存暴力 cosine。

```
构建期(Python)  MDX → chunk → trigram FTS  +  embedding BLOB ──┐
                                                                 ├─► bookwiki.sqlite
运行期(Node)    query ─► [trigram BM25 排名] ┐                  │
                         [向量 cosine 排名 ] ┴─ RRF ─► top-k ────┘
                              ▲ 文档框 GET /api/search / chat search_book 共用同一函数
```

规模假设:单本书 chunk 量级在数百~数千,512 维 float32 全库暴力 cosine 在 JS 中 < 5ms,无需近邻索引。

## 4. 模型与向量一致性(已核实)

- **模型**:`BAAI/bge-small-zh-v1.5`(512 维,中文检索强、体积小)。
  - 查询侧(Node):`@huggingface/transformers` 加载 `Xenova/bge-small-zh-v1.5`(已确认存在 transformers.js 版 ONNX)。
  - 构建侧(Python):`fastembed`,用 `add_custom_model()` **指向与查询侧同一份 ONNX**(`Xenova/bge-small-zh-v1.5` 的 `onnx/model.onnx`,mean pooling + L2 normalize),保证两侧字节级同权重 → 向量可比。
- **查询/文档前缀约定**(必须两侧一致):
  - query 文本前加指令 `为这个句子生成表示以用于检索相关文章：`。
  - passage(chunk)文本**不加**前缀。
- **归一化**:两侧都做 L2 normalize,cosine 退化为点积。
- **一致性自检**:测试中对同一字符串分别用两侧 embedding,断言 cosine ≈ 1.0(阈值如 ≥ 0.999),不达标即 fail(避免悄悄出错)。

## 5. 详细设计

### 5.1 数据库 schema 变更 — `bookwiki/indexer/sqlite_builder.py`

`_create_schema`:

1. FTS5 分词器改为 trigram:
   ```sql
   CREATE VIRTUAL TABLE fts_chunks USING fts5(
       text,
       heading_path,
       content='chunks',
       content_rowid='rowid',
       tokenize='trigram'           -- 由 unicode61 改为 trigram
   );
   ```
2. `chunks` 表新增向量列:`embedding BLOB`(float32 little-endian,512 维 = 2048 字节)。
3. 新增 `search_meta` 表记录 `embedding_model`、`embedding_dim`、`prefix_query`,供运行时校验(模型/维度不匹配则报错)。

### 5.2 构建期 embedding — `bookwiki/indexer/`

- 新增 `embedder.py`:封装 fastembed 初始化(自定义模型注册)与 `embed_passages(texts) -> list[bytes]`。
- `sqlite_builder._insert_chunks`:批量对 chunk 文本(passage,不加前缀)算向量,写入 `embedding` 列。
- 批处理以控制内存;失败(模型下载失败等)直接抛错,不静默跳过。
- 新增 Python 运行期依赖:`fastembed`(onnxruntime 系,不拉 torch)。

### 5.3 运行期共享检索 — `site-template/lib/rag.ts`

`searchChunks(query, limit, chapterId)` **签名不变**,内部升级为 hybrid:

1. **关键词路**:trigram FTS。重写 `toFtsQuery`:
   - 对每个空格分隔 term,长度 ≥ 3 → 作为 trigram 短语 `"term"`,多 term 以 `OR` 连接;
   - 任一 term 长度 < 3 → 该路对其走现有 `LIKE` 回退(trigram 索引会加速 `LIKE`)。
   - 保留现有 `try(FTS) catch(LIKE)` 兜底。
   - 取 top-N(如 N=30)及其 bm25 排名。
2. **语义路**:
   - 用 `@huggingface/transformers` 对 query(加查询前缀)算向量,模块级单例缓存 pipeline。
   - **扫全库**(有 `chapterId` 时为该章子集)`embedding` BLOB,JS 暴力 cosine(已 normalize → 点积),取 top-N。注意:语义路**不能**只在关键词候选里算,否则语义召回会被关键词结果限制,失去「按意思搜」的意义。
3. **融合**:对两路的**排名**做 RRF:`score(d) = Σ_path 1/(k + rank_path(d))`,`k=60`(标准默认)。按融合分降序取最终 `limit`。
4. 返回结构维持 `SearchChunk[]` 不变 → `search_book` 工具与文档 API 都复用。

新增检索辅助模块(保持 `rag.ts` 聚焦):
- `lib/embedding.ts`:transformers.js 单例 + `embedQuery(text): Float32Array`。
- `lib/vector.ts`:BLOB ↔ Float32Array、cosine/点积、top-N。
- `lib/fusion.ts`:RRF。

### 5.4 文档搜索 API — `site-template/app/api/search/route.ts`(整段重写)

- 移除 `createFromSource`。
- 实现自定义 `GET(request)`:读 `?query=` → 调 `searchChunks` → 映射为 fumadocs `SortedResult[]`:
  - 每个命中 page → 一条 `{ type: 'page', content: title, url }`;
  - 其下命中 chunk → `{ type: 'text', content: <片段>, url: /docs/<slug>#<anchor> }`;
  - `id` 用 chunk_id / page slug 保证稳定唯一。
- 前端 `RootProvider` 与默认搜索弹窗不改动;文档框输入侧配 debounce(降低每键一次向量计算)。

### 5.5 SQLite 连接(小优化,纳入本次)

`lib/sqlite.ts` 当前每次查询开/关只读连接。搜索框高频触发 + 每次要读向量 BLOB,改为**模块级缓存一个只读连接**(进程内复用),避免重复打开开销。

### 5.6 构建顺序

embedding 在 Python `build_sqlite_index`(`index_node` 调用)内一次性完成,产物仍是 `cfg.site_dir/.bookwiki/bookwiki.sqlite`,site 端 `next build` 不变。索引逻辑保持单一来源(Python)。

## 6. 融合算法:RRF

- 选 RRF 而非加权求和:对两路分数尺度(bm25 rank vs cosine)不敏感、无需调权重、实现简单稳健。
- 公式:`rrf(d) = Σ_{path ∈ {kw, vec}} 1 / (k + rank_path(d))`,`k = 60`。
- 仅出现在单路结果中的文档也参与(缺席路不贡献分量)。
- 未来若需偏置,可加每路权重系数;本期不做。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 两侧 embedding 不一致导致语义失效 | 两侧锁同一份 `Xenova/bge-small-zh-v1.5` ONNX;测试做 cosine≈1 自检 |
| trigram 对 < 3 字中文词(如「算法」)MATCH 无效 | 该路落 `LIKE`(trigram 索引加速);另有语义路兜召回 |
| transformers.js 冷启动加载模型(~40MB) | 假设 Next 以 node server 常驻,模型单例只首次加载;serverless 冷启成本在部署说明中标注 |
| trigram 索引体积约 3× | 单本书量级可忽略 |
| 向量 BLOB 增大 DB(~2KB/chunk) | 单本书几 MB,可接受;未来可 int8 量化 |
| fastembed 模型下载/离线环境 | 构建期预拉取模型缓存;失败直接报错(no-fallback) |

## 8. 测试计划

- **Python**
  - trigram 建索引后,断言「反向传播」「神经网络」能命中(刚才最小复现的反向断言)。
  - embedding 写入:断言每 chunk 有 512 维向量、`search_meta` 记录正确。
- **TypeScript**
  - `toFtsQuery`:≥3 字中文走 FTS、<3 字走 LIKE 的分支。
  - 向量工具:BLOB 往返、cosine 正确性、top-N。
  - RRF:已知排名输入 → 期望融合次序。
  - 语义召回端到端:只写「反向传播」的 chunk,用 query「梯度回传 / 误差逆传播」能召回。
  - `/api/search`:返回 `SortedResult` 形状校验 + 中文 query 命中。
  - 两侧 embedding 一致性 cosine≈1 自检。

## 9. 实施阶段(交由 writing-plans 细化)

1. 构建期:trigram + schema 变更 + `search_meta` + Python embedding(fastembed)+ 单测。
2. 运行期工具层:`embedding.ts` / `vector.ts` / `fusion.ts` + 单测。
3. `rag.ts` 升级为 hybrid + `toFtsQuery` 重写 + 单测(RAG 聊天此时即受益)。
4. `/api/search` 重写为自定义中文后端 + `sqlite.ts` 连接缓存 + 端到端测试。
5. 文档框 debounce、部署说明(node runtime / 模型缓存)。

## 10. 涉及文件清单

- `bookwiki/indexer/sqlite_builder.py`(schema、trigram、embedding 写入)
- `bookwiki/indexer/embedder.py`(新增,fastembed 封装)
- `site-template/lib/rag.ts`(hybrid、toFtsQuery)
- `site-template/lib/embedding.ts`(新增)
- `site-template/lib/vector.ts`(新增)
- `site-template/lib/fusion.ts`(新增)
- `site-template/lib/sqlite.ts`(连接缓存)
- `site-template/app/api/search/route.ts`(重写)
- `site-template/package.json`(`@huggingface/transformers`)、`pyproject.toml`(`fastembed`)
