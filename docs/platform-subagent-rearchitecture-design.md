# 平台招聘 Subagent 重构设计

状态：需求设计，尚未实施。目标是在不破坏现有 Opportunity Journey、HITL、SQLite
checkpoint 和平台风控保护的前提下，将 Boss 与小红书/Email 的平台能力从主 JobAgent
中收敛出去。

## 1. 问题与目标

当前主 JobAgent 直接注册 Boss、小红书、邮件、简历和聊天底层 Tool。随着 Boss 主动发简历、
小红书招人帖邮件投递和后台 HR 回复加入，主 Agent 很容易在不完整上下文中选择错误渠道或
错误工具。

目标：

```text
主 JobAgent：匹配判断、Journey、跨渠道路由、用户解释
BossRecruitingAgent：Boss 发现、会话、招呼、简历/回复提案
XhsRecruitingAgent：招人帖、JD/邮箱提取、邮件/简历提案
RecruitingActionExecutor：唯一外部写入口、HITL、回执和幂等
```

非目标：

- 不把平台 Cookie、securityId、resumeId、SMTP 凭据或抓取参数暴露给主 Agent；
- 不让 Subagent 自行自动发送消息、简历或邮件；
- 不以 Subagent 的自然语言总结作为 Journey 状态变更依据；
- 第一阶段不要求 Deep Agents async subagent；先以同步、可恢复的委派实现。

## 2. 推荐拓扑

```text
用户
 │
 ▼
JobAgent Supervisor
 ├─ 读取 Candidate Background / Job Search Profile
 ├─ 决定渠道与优先级
 ├─ 创建/更新 Opportunity Journey
 └─ 调用受限的渠道委派 Tool
       │
       ├───────────────┐
       ▼               ▼
BossRecruitingAgent  XhsRecruitingAgent
  仅 Boss Tool          仅 XHS/Email 准备 Tool
  产出 Proposal          产出 Proposal
       │               │
       └──────┬────────┘
              ▼
     RecruitingActionExecutor
       ├─ Proposal 校验
       ├─ HITL（渠道、对象、岗位、简历/正文）
       ├─ Boss / Email Adapter
       └─ Delivery Receipt + Journey 更新
```

关键决策：最终外部写操作不在 LLM Subagent 内执行。原因是当前 CLI 只有一层 HITL interrupt
恢复流程；把 HITL 放在嵌套图中会带来审批中断透传、重复恢复和审计归属不清的问题。Subagent
负责专业判断和提案，执行器负责确定性外发。

## 3. 职责边界

| 模块 | 可以做 | 不可以做 |
|---|---|---|
| JobAgent Supervisor | 判断匹配、选择渠道、管理 Journey、解释和调度 | 调平台私有接口、选择平台内部 token、直接发送 |
| BossRecruitingAgent | Boss 岗位/会话读取、JD 恢复、招呼/回复/简历发送提案 | SMTP、小红书抓取、最终外发 |
| XhsRecruitingAgent | 招人帖提取、OCR/JD/邮箱证据、定制邮件提案 | Boss 会话、最终 SMTP 发送 |
| RecruitingActionExecutor | 校验已批准提案、调用对应 Adapter、写回执与幂等 | 生成岗位匹配结论、杜撰文案或候选人事实 |
| 平台 Adapter | Chrome/CDP、HTTP/WS/SMTP 传输与平台回执归一化 | LLM 决策、Journey 阶段决策 |

## 4. 统一交付契约

两个渠道 Subagent 不返回“已完成”的自由文本，而是持久化并交付以下 Artifact。

```python
class RecruitingActionProposal:
    proposal_id: str
    journey_id: str
    channel: Literal["boss_chat", "email"]
    action: Literal["greet", "reply", "resume_delivery", "email_delivery"]
    recipient: RecipientRef                 # HR 会话或公开邮箱
    job_ref: JobRef
    resume_ref: ResumeVersionRef | None
    message_draft: str | None
    evidence_refs: list[str]
    idempotency_key: str
    expires_at: datetime | None
    policy_result: PolicyResult
```

执行器只接受：

1. 已绑定具体 Journey；
2. Artifact/Handoff 校验通过；
3. 渠道和 action 在允许的组合内；
4. 所有展示给用户的字段与提案一致；
5. 用户已通过 HITL 批准；
6. 幂等键尚未有 `confirmed` Delivery Receipt。

统一回执：

```python
class DeliveryReceipt:
    receipt_id: str
    proposal_id: str
    channel: Literal["boss_chat", "email"]
    status: Literal["confirmed", "unverified", "failed", "blocked"]
    transport_receipt: dict
    verified_at: datetime | None
    retry_policy: Literal["never_automatic", "manual_only"]
```

## 5. Agent 可见工具集

主 Agent 不再看见 `exchange/accept`、Boss upload、SMTP、XHS 下载等平台 Tool。它仅看见：

```text
delegate_boss_recruiting_task(journey_id, intent)
delegate_xhs_recruiting_task(journey_id, intent)
list_recruiting_action_proposals(journey_id)
execute_recruiting_action(proposal_id)
```

其中 `execute_recruiting_action` 是 HITL Tool。它的批准内容由已验证 Proposal 渲染，不能让
模型自由拼接：

```text
Boss · 陈女士 · 四川影目 · AI Agent 工程师
动作：发送简历
简历：李明_AI Agent工程师.pdf
依据：HR 于 2026-09-08 回复
```

Boss Subagent 的私有工具集：岗位发现、会话列表/历史、职位 URL/JD 恢复、招呼草稿、
HR 回复资格预检、Boss 可投递简历列表。

XHS Subagent 的私有工具集：招人帖搜索/保存、正文/图片/评论证据提取、JD 拆分、公开邮箱
提取、邮件和定制简历草稿。它不持有 SMTP 发送 Tool。

## 5.1 Proposal 传递与一致性保证

`proposal_id` 不是模型之间传递可变 JSON 的捷径，而是不可变 Proposal Snapshot 的数据库引用。
主 Agent、Subagent 和执行器都不信任彼此的自然语言复述；执行器永远重新读取 Snapshot。

```text
Subagent 写 Proposal v1（含 payload_hash）
  -> 校验通过，状态 READY
  -> 主 Agent 仅展示 summary + proposal_id
  -> execute_recruiting_action(proposal_id)
  -> Executor 原子领取 v1，校验 hash/状态/过期/幂等
  -> HITL 批准绑定 proposal_id + payload_hash
  -> Executor 路由并执行，写 Receipt
```

Proposal 状态机：

```text
DRAFT -> VALIDATING -> READY -> APPROVAL_PENDING -> EXECUTING -> CONFIRMED
                                      |                |             
                                      -> REJECTED      -> UNVERIFIED | FAILED | BLOCKED
READY / APPROVAL_PENDING -> EXPIRED
```

必须满足以下不变量：

1. `payload_hash` 覆盖渠道、动作、稳定收件人、岗位、简历版本/消息正文和幂等键；Proposal
   一旦进入 `READY` 不可原地修改，修改只能创建新版本；
2. HITL 恢复请求包含 `proposal_id + payload_hash`。如果审批前 Proposal 已过期、被替换或 hash
   不一致，执行器拒绝执行并要求重新准备；
3. `EXECUTING` 的领取使用 SQLite 事务和乐观版本/租约，同一 Proposal 只能有一个执行者；
4. 幂等键在 Receipt 表中唯一。进程重启、重复点击批准或后台 worker 重试都先查询 Receipt；
5. Adapter 临时凭据不在 Snapshot 中。执行器领取 Proposal 后，由目标渠道 Handler 重新读取
   当前会话/当前简历/当前邮箱配置；读取结果不再满足 Proposal 的稳定事实时，标为 `BLOCKED`；
6. `UNVERIFIED` 不自动重试；只允许用户显式创建新的 Proposal 重新发送。

因此 channel 不通过上下文注入或模型自由填写传递，而是 Proposal 的受限 enum 字段。主 Agent
的上下文只得到供用户理解的摘要；Boss `securityId`、`mid`、`encryptResumeId` 与邮件密码均不跨
Agent 传递。

## 6. Boss 与 XHS 工作流

### Boss

```text
Supervisor: “该岗位匹配，走 Boss”
  -> Boss Subagent: 发现/读取会话/生成 Proposal
  -> Proposal: greet | reply | resume_delivery
  -> Supervisor: 向用户解释 Proposal
  -> Action Executor: HITL -> Boss Adapter -> Receipt
```

Boss HR 后台监控将作为 Boss Subagent 的 worker 输入：它只产生 `reply_draft` 或
`resume_delivery` Proposal，不能绕过 Action Executor 自动外发。

### 小红书 → Email

```text
Supervisor: “检查 XHS 招人线索”
  -> XHS Subagent: 帖子/评论/OCR -> JD Evidence + Contact Channel
  -> 生成 Tailored Resume + Email Delivery Proposal
  -> Action Executor: HITL -> SMTP Adapter -> Receipt
```

## 7. 状态、并发与安全

- Artifact、Proposal、Receipt 和 Handoff 使用同一 SQLite Journey Store；平台临时凭据只在
  Adapter 内存中短时存在，不能写入 Proposal 或 LLM 上下文。
- 一个 Proposal 的幂等键至少包含 `channel + recipient stable ID + job identity + action +
  resume version/message hash`。
- 同一 Journey 的不同渠道可并行准备；同一 recipient 的外发执行串行化。
- 风控冷却是渠道级共享状态：Boss 被风控时 Boss Subagent 与 Executor 都快速拒绝，不影响
  XHS/Email。
- `unverified`、`failed`、过期 Proposal 和用户拒绝都只能人工重新准备，不自动重放。

## 8. 分阶段迁移

### Phase A：先建契约和执行器（建议第一切片）

1. 增加 Proposal/Receipt 表与 Artifact/Handoff 校验；
2. 将现有 Boss `greet`、`reply`、`resume_delivery` 包装为 Proposal 生产与执行两步；
3. 保持现有 Agent Tool 兼容，但在新路径中隐藏平台写工具；
4. 测试 HITL 内容、幂等、回执、过期、风控冷却和失败恢复。

### Phase B：BossRecruitingAgent

1. 建立仅包含 Boss 只读/提案 Tool 的专属图；
2. 主 Agent 通过 `delegate_boss_recruiting_task` 同步委派；
3. 以 Artifact/Handoff 结果推进 Journey；
4. 接入后台 HR Message Monitor，只能生成 Proposal。

### Phase C：XhsRecruitingAgent

1. 完成 XHS 招人帖到邮件 Proposal 的闭环；
2. 建立仅包含 XHS 证据/草稿 Tool 的专属图；
3. 接入统一 Action Executor 的 Email Adapter。

### Phase D：移除主 Agent 平台原始工具

迁移完成并通过兼容回归后，主 Agent 不再注册 Boss/XHS/SMTP 原始 Tool；只保留委派、
Proposal 查看和通用执行 Tool。

## 9. 验收标准

- 主 Agent 工具集不包含 Boss/XHS/SMTP 的平台原始写操作；
- Boss Subagent 无法调用 SMTP，XHS Subagent 无法调用 Boss Tool；
- 所有外发只有通过 `execute_recruiting_action` 的 HITL 后发生；
- 用户在批准框始终能看到渠道、对象、岗位、动作、简历文件名或邮件主题/附件；
- 任何 Subagent 自然语言“发送成功”不改变 Journey，只有 Receipt 可改变；
- 重试、并发和风控场景不会导致重复投递；
- 原有 Boss 打招呼、聊天回复、主动发简历和 XHS 邮件功能都有迁移回归测试。
