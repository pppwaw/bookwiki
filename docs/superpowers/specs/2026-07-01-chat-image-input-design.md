# AI Chat 图片输入(学生贴题)设计

日期:2026-07-01
状态:已确认,待实现

## 背景与目标

BookWiki 的 AI Chat 是基于书本 SQLite 索引做 RAG 的问答助手(grounded、带引用)。
希望学生能把一张**题目截图**贴进对话框提问,例如「这道题怎么做」。

整条链路本就是 Vercel AI SDK(前端 `@ai-sdk/react` 的 `useChat` + `DefaultChatTransport`,
后端 `convertToModelMessages` + `streamText` 经 OpenRouter),原生支持多模态,
默认模型 `google/gemma-4-31b-it` 支持图片输入。因此主要改动在 UI 与若干细节,而非 SDK 层。

## 需求边界

- 输入方式:**粘贴 (Ctrl+V)、上传按钮、拖拽** 三种都支持。
- 支持格式:png / jpeg / webp / gif。
- 上限:单张 ≤ 10MB,单条消息最多 4 张。
- 允许**纯图片提问**(有图无字也能发送)。
- 图片**不进历史**:只在当前会话内存有效,写 localStorage 时剥掉,发送前给出「刷新会丢图」提示。

### 明确不做(YAGNI)

图片压缩、图片持久化、服务端存图、OCR 管线、多图批处理、图片生成。纯透传给模型。

## 架构与组件

### 1) 视觉开关(服务端 → 客户端)

- 新增环境变量 `BOOKWIKI_CHAT_VISION`(`1`/`true`/`yes` 视为开启,其余/缺省为关闭)。写入 `.env.example`。
- `app/layout.tsx`(服务端组件)读取该 env,算出 `visionEnabled: boolean`,作为 prop 传给 `<AISearch visionEnabled={...}>`。
- `AISearch` 将 `visionEnabled` 放入 `HistoryContext`;`ChatComposer` 读取:
  - 开启 → 渲染图片入口(上传按钮 + 允许粘贴/拖拽)。
  - 关闭 → 入口完全隐藏,行为与现状完全一致(零回归)。
- 无需 `NEXT_PUBLIC_` 前缀,因为布尔值由服务端组件下传。

### 2) Composer 图片输入(`components/ai/search.tsx` 的 `ChatComposer`)

- 本地状态 `attachments: { id: string; file: File; url: string }[]`(`url` 为 `URL.createObjectURL` 预览地址)。
- 三种输入:
  - **粘贴**:textarea `onPaste` 读 `event.clipboardData` 中 `type` 以 `image/` 开头的项。
  - **上传**:隐藏 `<input type="file" accept="image/*" multiple>`,由输入框左侧图片图标按钮触发。
  - **拖拽**:form(或面板)`onDragOver` / `onDragLeave` / `onDrop`,拖入时显示高亮遮罩层。
- 校验(统一走一个 `addFiles(files)` 入口):
  - 类型不在 png/jpeg/webp/gif → 丢弃并内联提示。
  - 单张 > 10MB → 丢弃并提示。
  - 累计 > 4 张 → 截断并提示。
  - 提示为 Composer 内一行浅色文字,短暂展示,不阻断文字输入。
- 预览行:缩略图列表,每张带 × 删除(删除时 `revokeObjectURL`)。
- **丢图提示**:当 `attachments.length > 0` 时,预览行下方显示浅色小字:
  「图片仅本次会话有效,刷新或重开对话后会丢失」。
- 发送:`sendChatMessage({ text, files })`,`files` 为 `attachments.map(a => a.file)`(AI SDK 接受 `FileList`/`File[]`,转成 data URL 的 file part)。
  - 发送后清空 `attachments` 并逐个 `revokeObjectURL`。
- 发送按钮可用条件:`text` 非空 **或** 至少一张图片(且非 loading)。

### 3) 消息渲染(`Message` 的 user 分支)

- 现状:user 气泡仅渲染 `textContent(message)`,会丢弃 file part。
- 改为:在 user 气泡内同时渲染 `file` part 的图片缩略图(`part.type === 'file'` 且 `mediaType` 以 `image/` 开头,`url` 为 data/object URL),文字与图片并存。

### 4) 后端(`app/api/chat/route.ts`)

- `convertToModelMessages(uiMessages)` 本就支持 image file part → OpenRouter 多模态,**结构零改动**。
- 已知取舍:`convertHistory` 的 catch 兜底当前只保留 text part;仅在转换异常时触发,会丢图。保持现状,spec 记录此取舍。

### 5) 历史持久化(`lib/chat-history.ts`)

- 按「不入历史」:新增/复用一个 helper `stripFileParts(messages)`,移除每条消息 `parts` 中 `type === 'file'` 的项(保留 text/reasoning/tool 等)。
- 在写 localStorage 的路径(`saveMessages` 内部)应用 `stripFileParts`。内存态 `messages` 不变,保证同一会话内多轮追问模型仍看得到图。

## 数据流

学生贴图/拖入/上传 → 预览 → 发送 `{ text, files }`
→ `DefaultChatTransport` POST 到 `/api/chat`(图片以 data URL 进 file part)
→ `convertToModelMessages` → `streamText`(gemma 视觉)
→ 流式回答带引用 → 回合结束 `saveMessages` 用 `stripFileParts` 只存文字。

## 错误处理

- 不支持类型 / 超大 / 超张数 → Composer 内联短提示,不阻断文字。
- 模型侧报错(如拒图)→ 复用 `ChatList` 现有错误展示。

## 测试

- 单元:`stripFileParts`(去 file、保留 text/其它;空 parts 消息处理)。
- 单元:图片校验函数(类型/大小/张数边界)。
- 手动 / Playwright:开 `BOOKWIKI_CHAT_VISION` → 粘贴图片 → 验证请求体含 image file part 且 UI 显示缩略图;关闭开关 → 入口不出现。

## 涉及文件

- `.env.example` — 新增 `BOOKWIKI_CHAT_VISION`。
- `site-template/app/layout.tsx` — 读 env,下传 `visionEnabled`。
- `site-template/components/ai/search.tsx` — Composer 图片输入、user 图片渲染、context 增加 `visionEnabled`。
- `site-template/lib/chat-history.ts` — `stripFileParts` + 存储时应用。
- 测试文件(vitest)。
</content>
</invoke>
