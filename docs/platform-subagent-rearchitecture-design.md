# 平台招聘 Subagent 重构设计

状态：Phase A/B/C 已实施（Boss 与 XHS 子 Agent 已上线，见代码 `jobagent/agent.py`
的 `platform_subagents` 装配与本文第 11 节实现补充）。采用 DeepAgents 原生 Subagent HITL：
不引入跨 Agent Proposal 数据库、通用执行 Agent，也不把平台外发绕回主 Agent Tool。

## 1. 目标

主 JobAgent 不再直接看到 Boss、小红书和 SMTP 的平台原始 Tool。它负责岗位匹配、
Opportunity Journey、渠道选择和用户沟通；两个平台 Subagent 负责各自渠道的专业判断。

```text
JobAgent Supervisor
  ├─ BossRecruitingAgent
  └─ XhsRecruitingAgent
```

每个渠道 Subagent 自己持有渠道写 Tool 和 `interrupt_on`。它触发的暂停会作为根图
`interrupts` 返回；CLI 使用同一 root `thread_id` 执行 `Command(resume=...)`，框架从
子图暂停点继续。因此用户仍只看到一套终端批准交互，而外发责任留在渠道内。网页只读看板
属于低优先级 Future Work，不参与当前 HITL。

## 2. 最小拓扑

```text
用户
 │
 ▼
JobAgent Supervisor
 ├─ 匹配判断、Journey、选择渠道
 ├─ Deep Agents 原生 task Tool
 │
 ├─────────────┐
 ▼             ▼
BossAgent    XhsAgent
  Boss Tool      XHS/Email Tool
  [HITL]         [HITL]
 │             │
 ▼             ▼
Boss Adapter         Email Adapter
```

Boss/Email Adapter 是渠道 Tool 的内部确定性实现，不是第三个 Subagent、不是新的 LangGraph
node，也不使用模型。

## 3. 职责与权限

| 模块 | 可以做 | 不可以做 |
|---|---|---|
| JobAgent Supervisor | 匹配判断、Journey、选择/调度渠道、解释结果 | 调 Boss/SMTP 私有接口、直接生成平台凭据 |
| BossRecruitingAgent | Boss 发现、会话/JD 读取、招呼/回复/简历发送；每个写 Tool 均 HITL | SMTP、XHS、绕过 HITL |
| XhsRecruitingAgent | 招人帖/JD/邮箱证据、定制邮件及发送；邮件 Tool HITL | Boss、绕过 HITL |
| Boss/Email Adapter | 平台传输、回执读取和幂等落库 | 选择岗位/简历、生成文案、跨渠道路由 |

## 4. 前台跨 Agent 数据传递

不通过自由文本、长上下文注入或数据库 Artifact 在前台 Agent 间传递动作内容。

当前 Deep Agents 原生 `task` Tool 的参数只有 `subagent_type` 与 `description`。它会将任务描述
作为子 Agent 的 HumanMessage，并复制父图的非 private state。因此不应另写四个 delegate Tool。

```text
task(
  subagent_type="boss_recruiting",
  description='{"journey_id":"...", "intent":"prepare_resume_delivery"}'
)
```

任务 description 是最小委派信号，不是可信事实源。Boss/XHS Subagent 必须校验其中的
`journey_id`，并从受控 Journey Store/运行上下文读取实际 Job、Candidate Background 和策略。
静态渠道策略通过子 Agent system prompt 注入；动态平台凭据不通过 state 或 description 传递。

渠道内部使用的结构化上下文为：

```python
class ChannelTaskContext:
    session_id: str
    journey_id: str
    intent: str
    job_snapshot: JobRef
    candidate_snapshot: CandidateContextProjection
    user_policy: ReplyAndDeliveryPolicy
    conversation_id: str | None
```

渠道 Subagent 正常完成时返回：

```python
class ChannelTaskResult:
    summary: str
    discoveries: list[JobRef]
    warnings: list[str]
```

`channel` 不由模型作为普通参数传递：`subagent_type="boss_recruiting"` 固定为 `boss`，
`subagent_type="xhs_recruiting"` 固定为 `xhs_email`。平台私有凭据从不进入上述对象。

## 5. 渠道内预检（可选）

有些动作需要先展示可选简历或邮件草稿。渠道 Subagent 可以使用当前进程内、按 session
绑定、5–10 分钟有效的临时 `ChannelActionDraft` 保存已解析的平台字段：

```python
class ChannelActionDraft:
    draft_id: str
    session_id: str
    journey_id: str
    channel: Literal["boss", "xhs_email"]
    action: Literal["greet", "reply", "resume_delivery", "email_delivery"]
    approval_summary: ApprovalSummary
    prepared_payload: PrivatePreparedPayload  # 仅内存，包含临时平台字段
    expires_at: datetime
```

`approval_summary` 只包含用户应看到的信息；`prepared_payload` 可能含 Boss `securityId`、
`mid`、在线简历 ID 等，只能由创建它的同渠道写 Tool 读取。它不是跨 Agent 合同，也不是
主 Agent 的 `execute_channel_draft` 路由输入。

进程重启、草稿过期或用户拒绝时，Draft 丢弃并重新 prepare；这是安全失败，不自动重放。

## 6. DeepAgent 原生 Subagent HITL 集成

主 DeepAgent 只注册 Journey 等高层 Tool；传入 `subagents` 后 DeepAgents 自动注入 `task`：

```python
supervisor_tools = [
    *journey_tools,
]

create_deep_agent(
    model=model,
    tools=supervisor_tools,
    subagents=[boss_recruiting_spec, xhs_recruiting_spec],
    checkpointer=checkpointer,
)
```

传入 `subagents` 后框架自动注入 `task` Tool。每个 Subagent spec 配置自己的 `tools` 列表；
Boss spec 只给 Boss Tool（含 Boss 写 Tool），XHS spec 只给 XHS/Email Tool（含 SMTP 写 Tool）。
每个写 Tool 在本 Subagent 的 `interrupt_on` 显式配置；Subagent 配置会覆盖或继承根配置。

```text
Supervisor
  -> task(subagent_type="boss_recruiting", description=minimal intent + journey_id)
  -> BossAgent calls send_boss_resume_after_hr_reply(...)
  -> Boss Subagent HumanInTheLoopMiddleware interrupt
  -> root graph emits interrupts
  -> CLI renders action_requests and collects decisions
  -> root graph Command(resume={"decisions": [...]}) with same thread_id
  -> DeepAgents resumes Boss Subagent at its paused Tool
  -> Boss Adapter sends and writes Delivery Receipt to Journey
```

批准 UI 从框架的 `action_requests` / `review_configs` 渲染完整 Tool 名与参数；敏感字段须在
渲染前脱敏。平台 Tool 本身仍校验 HR、岗位、简历版本、session 和幂等键，不能信任模型回填。

## 7. 发送前后确定性校验

`interrupt_on` 只解决“是否得到用户批准”。每个渠道写 Tool/Adapter 在批准后仍必须检查：

```text
Draft 存在且未过期
Draft 属于当前 session / Journey / 渠道
当前用户选择的文件名与 Draft 一致
幂等记录中不存在 confirmed 的同一发送
Boss 风控冷却 / SMTP 配置状态正常
平台临时凭据仍有效
```

发送后只持久化长期事实：

```text
DeliveryReceipt
  - journey_id
  - channel
  - action
  - stable recipient ID / email
  - resume version or message hash
  - confirmed | unverified | failed | blocked
  - transport receipt
```

同一渠道、对象、岗位和简历版本/消息 hash 的 `confirmed` 回执用于幂等去重。`unverified`
与 `failed` 不自动重试，必须重新 prepare 并由用户批准。

## 8. 后台 HR 监控是另一条状态流

后台 Boss Message Monitor 不能使用前台内存 DraftStore，因为进程可能重启、也没有一个等待中的
用户会话。它产生的是持久化 `BossReplyQueue` 项：

```text
新 HR 消息
  -> 去重、意图分类、事实检查
  -> 生成 ReplyDraft
  -> 默认进入待审批队列
  -> 用户在 CLI 批准后，调用 BossRecruitingAgent 的回复 Tool 执行
```

只有用户明确按问题类别/公司/HR 开启自动回复时，Monitor 才可直接调用 BossActionHandler；
薪资谈判、入职承诺、面试时间、简历发送等永远进入待审批队列。

## 9. 分阶段迁移

### Phase A：Boss 先行

1. `ActionDraftStore`：内存 TTL、session/Journey 绑定、摘要 hash；
2. 将现有 Boss greet/reply/resume 逻辑作为 Boss 子 Agent 专属 Tool；
3. 主图使用原生 `task` 委派，并移除主图的 Boss 原始 Tool；
4. 在 Boss Subagent spec 配置写 Tool 的 `interrupt_on`，复用当前根图的 interrupt 渲染/恢复；
5. 回执继续写现有 Journey/Delivery 记录。

### Phase B：BossRecruitingAgent

1. 用专属 tool bundle 构建同步 Boss 子图；
2. 主 Agent 用原生 `task(subagent_type="boss_recruiting", description=...)` 调用；
3. 子图可在用户批准后自行执行 Boss 外发并返回 `ChannelTaskResult`；
4. 接入后台 HR Message Monitor 与 `BossReplyQueue`。

### Phase C：XhsRecruitingAgent

1. 完成 XHS 招人帖到邮件草稿闭环；
2. 用 XHS 专属 tool bundle 构建同步子图；
3. 在 XHS 子图为 SMTP 写 Tool 配置 `interrupt_on`，复用 HITL 和回执规范。

### Phase D：只在有真实需求时升级持久化 Handoff

仅当需要多进程 worker、跨机器执行、长时间异步任务恢复或人工跨天审批时，再将短时
ActionDraft 升级为 Journey Artifact/Handoff。当前不预先实现。

## 10. 验收标准

- 主 Agent 看不到 Boss/XHS/SMTP 原始写 Tool；
- Boss/XHS 子图工具集互斥；
- 前台 Draft 不落盘、不泄露平台临时凭据；
- 每次外发均由所属平台 Subagent 的 `interrupt_on` 暂停，且在根图统一展示/恢复；
- HITL 展示字段来自框架 `action_requests`，敏感字段经渲染层脱敏；
- 发送后只有 Delivery Receipt 更新 Journey；
- 错渠道 Draft、过期 Draft、重复批准、进程重启、Boss 冷却均安全失败；
- 后台 HR 监控使用持久化队列，不依赖前台 Draft。

## 11. 实现补充（2026-09-10）

Phase A/B/C 落地后，实际实现相对本设计新增了以下机制：

### 11.1 共享只读 Tool 镜像

XHS 子 Agent 除渠道专属 Tool 外，还镜像了根 Agent 的只读共享 Tool
（`_XHS_SHARED_TOOL_NAMES`）：`list_available_resume_pdfs`、`list_skills`、
`read_skill`。简历列表 Tool 让子 Agent 与邮件草稿服务读取**同一个**
`ResumeLibrary` 实例核实附件，杜绝子 Agent 扫描本地目录或报告其他路径的文件列表；
根 Agent 仍保留全部简历 Tool（含 `register_resume_pdf` 写入口）。

### 11.2 渠道工作流 Skill 确定性注入

`skills/xhs-recruitment-email/SKILL.md`（保存帖→核对岗位邮箱→核对附件→草稿→
审批的固定流程 + 失败恢复表）在 `build_job_agent` 装配时被读入、剥离 frontmatter、
拼进 xhs_recruiting 的 system prompt。注入不依赖模型主动读取；Skill 缺失时降级回
基础 prompt 并记 warning。子 Agent 的 `read_skill` 仅作运行时刷新（会话中途安装的
新版本）和读取其他 Skill 之用。

### 11.3 委派上下文契约

根 Agent 的 `<platform_subagent_policy>` 明确：Subagent 不继承主对话历史，task
任务描述必须自包含（帖子 URL 或 note_id、已确认公司/岗位、Journey ID、附件简历
文件名、用户逐字确认的邮件主题/正文、已有本地分析结论）。XHS 子 Agent prompt
对应要求：上下文缺失时先用 URL 读取帖子，无法补齐则返回 blocked 并列出缺失字段。

### 11.4 邮件草稿的逐字保存与溯源

`prepare_recruitment_email` 支持可选 `subject`/`body_text`：用户逐字确认的内容
原样落库不改写（`content_origin: user_confirmed`），未提供时回退默认模板。草稿
携带 `source_url` 与 `contact_evidence`（邮箱来源 body/图片 OCR 及置信级别），
HITL 批准预览与会话日志审阅均展示帖子链接与邮箱出处，供审批人核实收件人来源。
`send_recruitment_email` 只按 `draft_id` 从库重读草稿执行，不接受内容参数。

### 11.5 流程顺序的 fail-closed 保证

顺序不靠图编排硬编码，靠工具链数据依赖：未存帖 → `note_not_saved`，未选岗位 →
`position_not_selected`（均为结构化失败，非裸异常），无 verified 邮箱 →
`contact_missing`，简历不在库 → `resume_not_found`，草稿不存在 → `draft_not_found`，
已投递 → `already_submitted`。回归测试
`tests/test_xhs_email_drafts.py::test_prepare_draft_requires_note_saved_before_position_selected_before_draft`
钉住该性质。
