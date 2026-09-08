# 平台招聘 Subagent 重构设计

状态：需求设计，尚未实施。该设计优先服务当前单进程、单层 HITL 的 JobAgent；不预先引入
跨 Agent Proposal 数据库、嵌套 HITL 或第三个 LLM 执行 Agent。

## 1. 目标

主 JobAgent 不再直接看到 Boss、小红书和 SMTP 的平台原始 Tool。它负责岗位匹配、
Opportunity Journey、渠道选择和用户沟通；两个平台 Subagent 负责各自渠道的专业判断。

```text
JobAgent Supervisor
  ├─ BossRecruitingAgent
  └─ XhsRecruitingAgent
```

外发仍由当前主图的一层 HITL 完成，避免嵌套 Graph interrupt 的恢复复杂度。

## 2. 最小拓扑

```text
用户
 │
 ▼
JobAgent Supervisor
 ├─ 匹配判断、Journey、选择渠道
 ├─ Deep Agents 原生 task Tool
 ├─ execute_channel_draft  [HITL]
 │
 ├─────────────┐
 ▼             ▼
BossAgent    XhsAgent
  准备 Draft    准备 Draft
  仅 Boss Tool  仅 XHS/Email 准备 Tool
 │             │
 ▼             ▼
BossActionHandler   EmailActionHandler
```

`BossActionHandler` 与 `EmailActionHandler` 是普通 Python 处理器，不是第三个 Subagent、
不是新的 LangGraph node，也不使用模型。

## 3. 职责与权限

| 模块 | 可以做 | 不可以做 |
|---|---|---|
| JobAgent Supervisor | 匹配判断、Journey、选择/调度渠道、解释 Draft | 调 Boss/SMTP 私有接口、直接生成平台凭据 |
| BossRecruitingAgent | Boss 发现、会话/JD 读取、招呼/回复/简历发送草稿 | SMTP、XHS、最终外发 |
| XhsRecruitingAgent | 招人帖/JD/邮箱证据、定制邮件草稿 | Boss、最终 SMTP 外发 |
| BossActionHandler | 消费已批准 Boss Draft，调用 Boss Adapter、写回执 | 选择岗位/简历、生成文案 |
| EmailActionHandler | 消费已批准 Email Draft，调用 SMTP、写回执 | 选择邮箱/简历、生成文案 |

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

渠道 Subagent 返回：

```python
class ChannelTaskResult:
    summary: str
    action_draft_id: str | None
    discoveries: list[JobRef]
    warnings: list[str]
```

`channel` 不由模型作为普通参数传递：`subagent_type="boss_recruiting"` 固定为 `boss`，
`subagent_type="xhs_recruiting"` 固定为 `xhs_email`。平台私有凭据从不进入上述对象。

## 5. Channel Action Draft

前台 ActionDraft 是当前进程内、按 session 绑定、5–10 分钟有效的临时记录：

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

`approval_summary` 只包含用户应看到的信息：渠道、HR/邮箱、公司、岗位、简历文件名、
邮件主题/附件和理由。`prepared_payload` 可能含 Boss `securityId`、`mid`、在线简历 ID 等，
只能由同渠道 ActionHandler 读取，不返回模型、不写入日志或 Journey。

进程重启、草稿过期或用户拒绝时，Draft 丢弃并重新 prepare；这是安全失败，不自动重放。

## 6. DeepAgent 与 HITL 集成

主 DeepAgent 只注册高层 Tool：

```python
supervisor_tools = [
    list_current_action_drafts,
    execute_channel_draft,
    *journey_tools,
]

create_deep_agent(
    model=model,
    tools=supervisor_tools,
    subagents=[boss_recruiting_spec, xhs_recruiting_spec],
    middleware=[..., build_hitl_middleware()],
)
```

传入 `subagents` 后框架自动注入 `task` Tool。每个 Subagent spec 配置自己的 `tools` 列表；
Boss spec 只给 Boss 读取/prepare Tool，XHS spec 只给 XHS 读取/prepare Tool。两者都不配置
最终外发 Tool。`execute_channel_draft` 是主图唯一的外发 Tool，加入现有 `_HITL_TOOLS`。

它不是第三个 Subagent：只从内存 Draft 读取已固定的 `channel`，用 Python 映射调用
`BossActionHandler` 或 `EmailActionHandler`。模型无法把 Boss Draft 改路由到 Email。

```text
BossAgent prepare
  -> DraftStore.put(session_id, draft)
  -> 主 Agent 展示候选简历或邮件草稿
  -> execute_channel_draft(draft_id, approval_summary_hash)
  -> HumanInTheLoopMiddleware interrupt
  -> BossActionHandler.execute(draft)
  -> Delivery Receipt 写入 Journey
```

HITL 描述不能信任模型回填的 HR 或文件名。`build_hitl_middleware()` 增加 DraftPreviewResolver：
它按 `draft_id` 从内存 DraftStore 读取真实 `approval_summary`，并校验 `session_id` 与摘要 hash，
再渲染批准框。

## 7. 发送前后确定性校验

`interrupt_on` 只解决“是否得到用户批准”。每个渠道 Handler 在批准后仍必须检查：

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
  -> 用户在网页/CLI 批准后，转换为前台 ChannelActionDraft 执行
```

只有用户明确按问题类别/公司/HR 开启自动回复时，Monitor 才可直接调用 BossActionHandler；
薪资谈判、入职承诺、面试时间、简历发送等永远进入待审批队列。

## 9. 分阶段迁移

### Phase A：Boss 先行

1. `ActionDraftStore`：内存 TTL、session/Journey 绑定、摘要 hash；
2. 将现有 Boss greet/reply/resume 逻辑包装为 `prepare_*` + `execute_channel_draft`；
3. 主图使用原生 `task` 委派，并移除主图的 Boss 原始写 Tool；
4. 复用当前 `HumanInTheLoopMiddleware`，增加动态 Draft 审批预览；
5. 回执继续写现有 Journey/Delivery 记录。

### Phase B：BossRecruitingAgent

1. 用专属 tool bundle 构建同步 Boss 子图；
2. 主 Agent 用原生 `task(subagent_type="boss_recruiting", description=...)` 调用；
3. 子图只产生 `ChannelTaskResult` 和 ActionDraft，不执行最终外发；
4. 接入后台 HR Message Monitor 与 `BossReplyQueue`。

### Phase C：XhsRecruitingAgent

1. 完成 XHS 招人帖到邮件草稿闭环；
2. 用 XHS 专属 tool bundle 构建同步子图；
3. 添加 `execute_xhs_action`，复用 HITL 和回执规范。

### Phase D：只在有真实需求时升级持久化 Handoff

仅当需要多进程 worker、跨机器执行、长时间异步任务恢复或人工跨天审批时，再将短时
ActionDraft 升级为 Journey Artifact/Handoff。当前不预先实现。

## 10. 验收标准

- 主 Agent 看不到 Boss/XHS/SMTP 原始写 Tool；
- Boss/XHS 子图工具集互斥；
- 前台 Draft 不落盘、不泄露平台临时凭据；
- 每次外发只经过当前一层 `interrupt_on`；
- HITL 展示字段来自 DraftStore，不来自模型自然语言；
- 发送后只有 Delivery Receipt 更新 Journey；
- 错渠道 Draft、过期 Draft、重复批准、进程重启、Boss 冷却均安全失败；
- 后台 HR 监控使用持久化队列，不依赖前台 Draft。
