# JobAgent 网页端对齐 Office AI 前端——需求与功能设计方案

状态：v2 设计稿（2026-09-21）。v2 在 v1 基础上细化功能设计并修订不足点（见 §0.2）。
本文只做设计，不含代码改动。

## 0. 背景

### 0.1 现状与差距

office-ai 前端（`/opt/office-ai/ui`）与 jobagent 网页端（`jobagent/ui`）同源：
两者 `package.json` 都叫 `jobagent-ui`，同为 React 19 + Vite + TypeScript 单文件
`App.tsx` + `styles.css`。office-ai 是这条线的演进展（App.tsx 765 行），jobagent
当前是精简版（245 行，视图：`journeys`（默认）/ `assistant`）。

| 能力 | office-ai 前端 | jobagent 前端现状 |
|---|---|---|
| 默认落地页 | 主页 WelcomeView（问候语 + 任务输入框 + 热门任务 + 灵感卡片） | journeys 列表页 |
| 新建任务流 | 输入/点卡片 → 创建会话 → 首条消息自动发出 → 进入对话窗 | 无主页；助手页有 pendingPrompt 机制但无主页入口 |
| 对话窗 | SSE 流式 + 过程步骤（可折叠）+ 计时 | 阻塞式 POST，仅「Agent 思考中…」打字态 |
| 断线恢复 | 轮次在服务端后台继续，`/events/live` 重放 | 无（请求断开 = 客户端拿不到增量） |
| 侧栏 | 新建任务导航 + 最近工作（搜索过滤、删除） | 助手页内嵌历史列表（分页、批量删除） |
| 附件 | 通用 `api/uploads`（≤20MB） | 仅 `/api/resumes/upload`（简历库） |
| 模型切换 | `api/models` GET/POST 下拉 | 无 |
| HITL 呈现 | interrupt 事件 → 过程步骤展示 | 无（中断回合会以错误形式暴露，见 §2.4） |

关键后端事实（已核实）：

- `AgentStreamEvent.kind ∈ {status, token, thinking, tool, interrupt, done}`
  （`jobagent/agent.py`）——SSE 端点的协议必须按这套真实事件设计；
- `reply()` 收集 token，**无可见回复时抛 `RuntimeError`**——HITL 中断的回合
  会走到这里，当前 web 端会显示成「Agent 暂时无法回答」，这是 v1 未识别的
  体验缺陷；
- `platform_hint="web"` 只改提示词元数据层，**不裁剪工具集**——web Agent 与
  CLI 共享完整工具 bundle，在服务器部署上 Boss/XHS 工具没有浏览器与本地数据，
  调用时会走各工具自身的结构化失败路径；
- journey 通道的 `web_conversations` **没有删除端点**（assistant / office_ai
  有）。

### 0.2 v2 相对 v1 的修订（不足点改进）

| # | v1 不足 | v2 修订 |
|---|---|---|
| 1 | SSE 只列了事件类型名，无契约 | §3.1 给出完整事件协议：字段、与 `AgentStreamEvent` 的映射、终止/心跳/并发防护、**服务端完整落库约束** |
| 2 | 未识别 HITL 中断在 web 的呈现问题 | §2.4：中断 ≠ 错误；网页端只读呈现 + 引导去 CLI 审批；不提供网页批准/恢复（安全边界） |
| 3 | 「最近对话合并 assistant+journey」不可行（journey 会话按 journey 逐个拉是 N+1） | §2.5：v1 侧栏只聚合 assistant 通道；跨通道聚合端点列为 P2 |
| 4 | 灵感卡片未考虑部署环境差异（服务器无 Boss/Chrome/本地数据） | §2.2：卡片带 `requires` 能力标注 + 能力探测端点，环境不可达时降级 |
| 5 | 消息渲染策略未定义（web hint 允许 Markdown） | §2.3：v1 纯文本 + 换行（同 office-ai），受控 Markdown 列 P2；禁止未消毒 HTML 注入 |
| 6 | 刷新丢视图、会话切换无状态管理设计 | §2.6：hash 路由 + conversationStore；刷新可恢复 |
| 7 | 新建任务连点会建重复会话 | §2.2：创建单飞（pending 禁用） |
| 8 | 错误/空态未成矩阵 | §2.3 错误与空态矩阵 |
| 9 | Journey 任务按钮「预填 prompt 送入对话」语义含糊 | §2.7：预填输入框不自动发送；无活跃会话时先建任务同名会话 |
| 10 | 未提前端测试与部署构建 | §4：测试策略（组件单测 + 手测清单）与 dist 构建部署 |

## 1. 需求设计

- **R1 主页（P0）**：借用 office-ai `WelcomeView` 骨架；灵感卡片改为求职域
  内容（清单见 §2.2）；「热门任务」与「灵感卡片」合并为一个卡片区。
- **R2 新建任务 → 跳转对话（P0）**：输入或点卡片 → 创建会话（标题取首条输入
  前 20 字）→ 自动发出首条消息 → 进入对话窗；创建过程单飞防重。
- **R3 对话窗（P0）**：流式 + 过程步骤 + 中断/错误分级呈现（§2.3/§2.4）；
  后端 SSE 端点（§3.1）；阻塞旧端点保留为降级路径。
- **R4 全局侧栏（P1）**：导航「新建任务｜求职旅程｜简历库」+ 最近对话
  （v1 仅 assistant 通道，§2.5）。
- **R5 三通道归位（P0，决策）**：主页新建任务 → `assistant` 通道；
  Journey 详情 → `journey` 通道；`office_ai` 通道 API 保留、前端不露出。
- **R6 附件（P1）**：composer 附件走既有 `/api/resumes/upload`（20MB、后缀
  白名单、413 已有测试），不引入通用 uploads。
- **R7 断线恢复（P2）**：`/events/live` 重放需要轮次总线；本期 SSE 断开时
  UI 如实提示「本轮仍在服务端执行，结果稍后可在历史中查看」（§3.1 的落库
  约束保证这句话为真）。
- **R8 HITL 网页边界（P0）**：外部写操作（Boss 发送、SMTP、站内信）永远
  人工审批；网页端遇到 interrupt 只呈现状态，不提供批准/修改/恢复入口，
  审批动作仅在 CLI 完成（对齐 future-work.md 的网页只读看板定位）。
- **R9 内容渲染（P1）**：v1 纯文本 + 保留换行；P2 评估受控 Markdown 子集。

## 2. 功能设计

### 2.1 信息架构与路由

```text
#/                      主页（默认）：composer + 求职灵感卡片
#/assistant/:id         求职助手对话窗
#/journeys              Journey 列表（保留现有抽取/匹配/OCR 流程）
#/journeys/:id          Journey 详情（上下文卡 + 任务按钮 + 对话窗复用）
侧栏：新建任务 ｜ 求职旅程 ｜ 简历库 ｜ 最近对话（assistant 通道）
```

- hash 路由（免服务端回退配置），`pushState` 可后置；**刷新后按 URL 恢复
  视图与会话**（v1 现状刷新回列表页，丢上下文）。
- `View` 状态：`"home" | "assistant" | "journeys" | "journeyDetail"`；
  `assistant` 视图无 `:id` 且无活跃会话时渲染主页内容。

### 2.2 主页（WelcomeView 改造）

组件结构照搬 office-ai：问候语 H1 → composer → 灵感卡片区。

**composer 行为**：

- textarea：Enter 发送、Shift+Enter 换行；空输入禁用发送。
- 附件：📎 按钮走 `/api/resumes/upload`，成功后输入框上方显示可移除的
  附件条（同 office-ai 交互），发送时在消息尾追加「（附件简历：xxx.pdf）」。
- **创建单飞**：`onStart` 期间发送按钮 pending，防止连点建重复会话。
- **空画像引导**：`/api/profile` 返回 `source: "empty"` 时，主页顶部显示
  一条非阻断提示「先完善简历/背景，Agent 的分析会更准」+ 简历库入口，
  不阻塞直接对话。

**求职域灵感卡片**（`{ title, sub, tag, prompt, requires? }`，8 张）：

| 卡片 | tag | requires | prompt 要点 |
|---|---|---|---|
| 从 JD 创建求职旅程 | 建档 | — | 引导粘贴 JD 文本；截图则走 `/api/journey-drafts/ocr` 后带出结构化结果 |
| 岗位匹配度分析 | 分析 | — | 匹配度/优势/缺口/投递建议，落到 Match Analysis 结构 |
| 定制这份简历 | 简历 | resume | 基于简历库 + 指定 JD 出定制建议与保留事实边界 |
| 模拟面试 | 面试 | journey | 追问式模拟，围绕指定 Journey |
| 跟进 HR 回复 | 跟进 | boss | 汇总待跟进会话、起草跟进话术（只读） |
| 打招呼语打磨 | 转化 | boss | 基于 JD 定制打招呼语（草稿，不发送） |
| 面经调研 | 情报 | web | 检索面经、汇总高频考点 |
| 求职进度盘点 | 复盘 | — | 汇总 Journey 状态与下一步建议 |

**环境降级（v1 不足 #4 的修订）**：新增 `GET /api/capabilities`，返回
`{ resume_library: bool, boss_data: bool, journeys: n }`（boss_data =
本地存在 Boss 会话/cookie 数据）。前端按能力隐藏或置灰 `requires` 不满足的
卡片（置灰 + tooltip「需要本地 Boss 数据，当前为服务器部署」）。
本地 dev 部署全部可用；服务器部署默认只有纯分析类卡片。

**「从 JD 创建旅程」卡片专属流程**（区别于其他卡片的直接发消息）：

```text
点击卡片 → 弹层二选一：粘贴 JD 文本 ｜ 上传 JD 截图
  → 文本：POST /api/journey-drafts/extract 结构化预填 → 用户确认 → 走 R2 流程发首条消息
  → 截图：POST /api/journey-drafts/ocr → 同上
  → （可选）match 预览匹配度后一键创建 Journey
```

### 2.3 对话窗

**布局**（同 office-ai）：消息区 + 可折叠过程步骤区 + composer。

- 消息气泡：user 右 / assistant 左；assistant 回复前若有过程步骤，折叠条
  显示「已思考 · N 步 · 12s」，展开后按类型渲染 thinking（暗色斜体）/
  tool（单行摘要 + 状态图标）/ status / interrupt / error。
- 计时：回合进行中显示经过时间（office-ai `ensureTicker` 移植）。
- 时间戳：气泡 hover 显示 `created_at`，不占版面（现状消息表已有字段）。
- 历史加载：进入会话一次性拉全量消息（现状契约）；超过 200 条后前端
  「加载更早」分页列为 P2（后端 before 游标）。

**错误与空态矩阵**：

| 场景 | 呈现 |
|---|---|
| HITL interrupt（§2.4） | 过程区终止步骤「⏸ 需要人工确认（请在 CLI 审批）」；消息区提示本轮已暂停在确认点 |
| LLM/后端异常 | assistant 气泡安全文案（沿用 `_reply_from_web_agent` 语义），过程区不暴露堆栈 |
| 空回复（RuntimeError: no visible message） | 「本轮没有生成可见回复」，可重新输入 |
| SSE 断开 | 顶部黄条「连接已断开，本轮仍在服务端执行，结果稍后可在历史中查看」+「重新打开会话」按钮（重拉消息） |
| 409 上一轮进行中 | 输入框上方内联提示，不打断已输入内容 |
| 会话不存在（404） | 空态页「会话不存在或已删除」+ 返回主页 |

**并发防护**：前端 busy 禁发 + 后端每会话 in-flight 锁（§3.1）双保险。

**内容渲染**：v1 纯文本 + `\n` 保留；React 文本节点渲染，**禁止
`dangerouslySetInnerHTML`**；P2 若引入 Markdown，用受控子集 + 消毒管线，
且 HR 消息/JD 等外部文本永不走富文本渲染（对齐 prompt 注入防线）。

### 2.4 HITL 在网页端的边界（v1 不足 #2 的修订）

已核实的行为：外部写工具触发审批时，流以 `interrupt` 事件表达；若调用方
只收 token（现在的 `reply()`），该回合无可见回复并抛 RuntimeError——web
会把「等待审批」误显示为「无法回答」。

设计约束：

1. SSE 端点把 `interrupt` 映射为一级事件；前端在过程区以「⏸ 需要人工确认」
   终止步骤呈现，消息区提示「请到 CLI 运行 `jobagent chat` 完成审批；审批后
   回到本页刷新查看后续」。
2. **网页端不提供批准/修改/恢复操作**（R8 安全边界，与 future-work.md
   「CLI 正在等待审批时仅显示等待状态」一致）。检查点已持久化 pending HITL，
   后续如做网页审批需单独立项（威胁模型：当前 API 无鉴权）。
3. interrupt 与 error 在 UI 上必须可区分：前者是等待态（蓝色），后者是
   失败态（红色）。

### 2.5 侧栏（v1 不足 #3 的修订）

- 导航区：新建任务 / 求职旅程 / 简历库。
- 「最近对话」v1 仅聚合 **assistant 通道**（`limit=30` + 标题前端过滤 +
  删除）；Journey 会话仍留在各自详情页内管理。
  理由：journey 会话按 journey 逐个拉取是 N+1；跨通道聚合需要后端
  `GET /api/conversations/recent`（UNION 两表 + channel 徽标）→ 列 P2。
- 删除走既有 `DELETE /api/assistant/conversations`；删除确认弹层注明
  「仅删除网页历史，不影响 Agent 记忆」。

### 2.6 状态管理与路由（v1 不足 #6 的修订）

- `conversationStore`：`Map<id, {messages, steps, busy, liveText, startedAt}>`，
  移植 office-ai 的 patch/get 形状；切换会话不丢进行中回合的实时状态。
- 路由变化即 `store` 读写，不重新 fetch 活跃会话。
- 多标签页：不做跨标签同步（P2，可后置为 BroadcastChannel）。

### 2.7 Journey 详情页

- 对话区替换为共享 `ChatWindow`（journey 通道，`system_prompt_override`
  传参路径保留，岗位隔离回归测试不回退）。
- **任务按钮（岗位调研 / 简历匹配分析 / Mock Interview）**：点击 → 若无
  活跃会话，先创建以任务命名的会话；随后**把预填 prompt 放入输入框而不
  自动发送**（用户可补充上下文，如「重点看数据库方向」后手动发送）——
  替换现状的 `PlaceholderDialog` 占位。
- Journey 上下文卡（JD、Tasks/Artifacts 计数）保留现状。

### 2.8 前端代码组织（借结构时拆文件）

```text
jobagent/ui/src/
├── App.tsx              # 外壳 + 路由（薄）
├── lib/api.ts           # fetch 封装 + 类型
├── lib/stream.ts        # SSE 解析 / startRun / applyTurnEvent / ticker
├── lib/store.ts         # conversationStore
├── components/
│   ├── Welcome.tsx      # 主页 + 卡片数据（含 requires）
│   ├── ChatWindow.tsx   # 对话窗（assistant / journey 两处复用）
│   ├── Sidebar.tsx      # 导航 + 最近对话
│   └── ...（JourneyList / JourneyDetail / ResumePanel 平移）
```

复用映射（office-ai `App.tsx` → 本仓）：`WelcomeView`→`Welcome.tsx`；
`startRun`/SSE 循环→`lib/stream.ts`；`OfficeRecent`→`Sidebar.tsx`；
`App` 侧栏外壳→`App.tsx`。

## 3. 后端配套

### 3.1 A1：SSE 流式端点（P0）

`POST /api/assistant/conversations/{id}/messages/stream`，
响应 `text/event-stream`。

**事件协议**（与 `AgentStreamEvent.kind` 映射）：

| SSE 事件 | 来源 | payload |
|---|---|---|
| `status` / `thinking` / `tool` | 同名 kind | `{type, seq, text, data?}` |
| `interrupt` | `interrupt` kind | `{type, seq, text}`（含审批指引文案） |
| `delta` | `token` kind | `{type, seq, text}`（回答增量） |
| `message` | done 后落库返回 | `{type, message:{role:"assistant",content,created_at}}` |
| `error` | 异常 | `{type, seq, text}`（安全文案，不含堆栈） |
| 心跳 | 定时器 | 注释行 `: ping`（15s，防中间层空闲断开） |

**执行与落库约束（本设计的核心保证）**：

- Agent 消费放在**独立 asyncio task**：持续读 `stream_reply()` 直到 `done`
  并把完整回复落库 `assistant_messages`；SSE 生成器只从该 task 的事件队列
  转发给客户端。**客户端断开不影响执行与落库**——这使 §2.3 的断开文案
  （「结果稍后可在历史中查看」）为真。
- 错误隔离沿用 `_reply_from_web_agent` 语义的流式版：异常 → `error` 事件 +
  落库安全文案， finally 关闭 agent。
- **并发防护**：每会话 in-flight 标记（进程内 dict）；进行中再发 →
  `409 {"detail": "上一轮回复仍在进行"}`。
- 错误事件不带堆栈与内部细节（对齐既有 review 结论）。

### 3.2 其余配套端点

| # | 端点 | 说明 | 优先级 |
|---|---|---|---|
| A2 | journey 通道流式端点 | 同 A1 + `system_prompt_override`；同会话并发防护 | P1 |
| A3 | 会话标题生成 | 创建时取首条消息前 20 字，替代固定「新求职对话」 | P0（随 S1） |
| A4 | `GET /api/capabilities` | 灵感卡片环境降级依据（§2.2） | P1 |
| A5 | `/events/live` 重放 | 需轮次总线（进程模型改动），依赖 A1 的 task 化先行验证 | P2 |
| A6 | `/api/models` | 模型下拉 | P2 |
| A7 | journey 会话删除端点 | 现状缺失；随侧栏/详情删除需求启动 | P2 |

安全约束不变：API 无鉴权 → 服务只绑 127.0.0.1；SSE 不新增写文件面；
interrupt 只读呈现（§2.4）；错误事件不泄露内部信息。

## 4. 工程与部署

- **测试**：
  - 后端：A1 事件协议单测（fake agent 产事件流 → TestClient 断言 SSE 帧
    顺序与落库完整性、断开不丢落库、409 并发、interrupt/error 分类）；
  - 前端：沿用 `draft-regression.test.mjs` 模式加纯函数单测（SSE 帧解析、
    步骤归并、卡片 requires 过滤）；组件交互靠每切片手测清单验收；
  - 手测清单随切片给出（S1：卡片→建会话→跳转→首条消息；S3：断网恢复、
    interrupt 呈现、连点防重）。
- **构建部署**：`npm run build` → `jobagent/ui/dist` 随 tar 部署，web.py
  既有 `UI_DIST.is_dir()` 挂载自动生效；本地构建产物入 tar（服务器不装
  node）；`systemctl restart jobclaw` 生效。

## 5. 实施切片与验收

| 切片 | 内容 | 验收要点 |
|---|---|---|
| S1 | 主页 + 求职灵感卡片 + 新建跳转 + 标题生成（A3，先用阻塞端点） | 点卡片单飞创建会话并自动发首条消息 → 落到对话窗；空画像提示；刷新回主页 |
| S2 | hash 路由 + 侧栏改造 + capabilities（A4）+ 卡片降级 | URL 直达恢复视图；最近对话可搜/删；服务器部署下 boss 类卡片置灰 |
| S3 | A1 SSE + 对话窗流式与过程步骤 + HITL 呈现 + 并发 409 | 增量渲染；步骤折叠；断开后历史可见本轮结果；interrupt 蓝色等待态与 error 红色可区分；旧端点回归不破 |
| S4 | Journey 详情复用 ChatWindow（A2）+ 任务按钮预填 | 隔离 prompt 回归绿；任务按钮预填不自动发；interrupt 路径同 S3 |
| S5 | P2 项（live 重放 / 模型切换 / Markdown 渲染 / 跨通道聚合） | 按需另评 |

## 6. 风险与开放决策点

1. **流式排期**：S1 可先用阻塞端点最快见效；「对话窗口原汁原味」要 S3。
   建议 S1→S3 连续交付。
2. **interrupt 后的恢复入口**：本期引导去 CLI。若产品上接受网页审批，
   需先给 API 加鉴权并单独立项（安全前置）。
3. **web Agent 工具集是否裁剪**：`platform_hint` 不裁工具，服务器部署上
   Boss/XHS 工具调用只会得到结构化失败。可选优化：`build_job_agent` 支持
   web 环境工具白名单（纯本地分析 + 检索），减少无效工具注入——列 P2，
   需与 prompt 门控表联动。
4. **长会话性能**：全量拉取 + 纯文本渲染在数百条内无压力；分页与虚拟滚动
   触发条件：单会话 >500 条实测卡顿再实施。
5. **office_ai 通道不露出**（v1 决策维持）：避免同一前端两个「新建任务」
   语义打架。
