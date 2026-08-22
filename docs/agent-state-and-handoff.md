# JobAgent 状态管理与 Agent 交接校验设计

> 状态：目标设计。第一阶段由单一 JobAgent 使用同一套 Task Run 和 Artifact 模型；拆分
> Deep Agents 子 Agent 后增加 Handoff，但不改变业务产物和校验规则。

## 1. 核心原则

聊天消息不是工作流状态，子 Agent 的自然语言“已完成”也不是完成证据。系统只根据持久化
状态、结构化产物和校验结果推进 Opportunity Journey。

```text
Task Contract
  -> Task Run
  -> versioned Artifact(s)
  -> deterministic validation
  -> semantic/policy validation
  -> Handoff acceptance
  -> next Task Run
```

- Supervisor 负责编排、预算、依赖、重试和 HITL，不替专业 Agent 静默修补产物。
- Producer 只能提交结构化 Artifact 和 Handoff Manifest，不能用自由文本宣布完成。
- Consumer 只读取已接受的 Artifact；被拒绝的产物返回 Producer 修订。
- Artifact 不可原地覆盖。修订产生新版本，并通过 `supersedes` 指向旧版本。
- 所有写入使用幂等键和乐观版本，避免重试或并发 Agent 重复创建任务、投递或日历事件。

## 2. 四层状态

### 2.1 Opportunity Journey

业务阶段保持粗粒度：

```text
TARGETED -> APPLIED -> INTERVIEWING -> CLOSED
```

它只描述现实求职进展，不表示内部 Agent 是否正在运行。

### 2.2 Journey Workstream

每个 Journey 分别维护研究、准备、简历、内推、投递和面试工作流：

```text
NOT_STARTED -> READY -> RUNNING
RUNNING -> WAITING_USER | COMPLETED | FAILED | CANCELLED
WAITING_USER -> READY | CANCELLED
FAILED -> READY (显式重试)
```

一个工作流完成不自动推进其他工作流。例如面试准备完成不代表已经投递。

### 2.3 Task Run

一次具体 Agent/Tool 执行对应一个 Task Run：

```python
class TaskRun:
    run_id: str
    journey_id: str
    workstream: str
    task_type: str
    assigned_agent: str
    status: str
    input_artifact_ids: list[str]
    output_artifact_ids: list[str]
    attempt: int
    idempotency_key: str
    budget: dict
    checkpoint_ref: str | None
    error: dict | None
    created_at: datetime
    updated_at: datetime
```

Task Run 状态：

```text
PENDING -> RUNNING -> VALIDATING -> SUCCEEDED
                   -> REVISION_REQUIRED -> RUNNING
RUNNING -> WAITING_INPUT | FAILED | CANCELLED
```

只有 Handoff/Artifact 校验通过后才能进入 `SUCCEEDED`。

### 2.4 Artifact 与 Handoff

```python
class ArtifactEnvelope:
    artifact_id: str
    journey_id: str
    artifact_type: str
    schema_version: str
    version: int
    status: str
    created_by_run_id: str
    content_ref: str
    content_hash: str
    provenance_refs: list[str]
    supersedes: str | None

class HandoffManifest:
    handoff_id: str
    journey_id: str
    producer_run_id: str
    consumer_task_type: str
    artifact_ids: list[str]
    contract_version: str
    validation_report_id: str | None
    status: str
```

Artifact 状态：

```text
DRAFT -> VALIDATING -> ACCEPTED
                    -> REJECTED -> SUPERSEDED
```

Handoff 状态：

```text
PROPOSED -> VALIDATING -> ACCEPTED
                       -> REVISION_REQUIRED | REJECTED
```

## 3. 交接校验流水线

每次交接按固定顺序执行：

1. **Schema Gate**：类型、必填字段、schema 版本、枚举和大小限制正确。
2. **Identity Gate**：所有 Artifact 属于同一个 Journey；输入版本未被淘汰；内容哈希有效。
3. **Provenance Gate**：关键结论能追溯到 Raw Source Snapshot、Source Image、JD 或已确认
   Candidate Background。
4. **Deterministic Gate**：去重、数量、时间范围、预算、状态前置条件和幂等键满足规则。
5. **Semantic Gate**：LLM 对相关性、完整性、矛盾和缺失项输出结构化 Validation Report；
   LLM 不能覆盖前四个确定性校验结果。
6. **Policy Gate**：证据等级、简历真实性、外部动作授权、隐私和平台风控满足政策。
7. **Consumer Acceptance**：目标 Agent 根据自己的输入契约确认可消费；否则返回明确的
   `RevisionRequest`，不得自行猜测或静默补字段。

校验结果必须包含：

```python
class ValidationReport:
    valid: bool
    validator_version: str
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
    checked_artifact_ids: list[str]
    recommended_action: str
```

同一错误最多自动修订固定次数。超过次数、出现事实冲突或需要新增用户事实时转为
`WAITING_USER`，不能让 Agent 无限互相 transfer。

## 4. 各 Agent 的交接契约

### 4.1 Interview Research -> Interview Preparation

必须提交：

- A/B Interview Evidence，或者明确的 Evidence Gap Report；
- 每条证据引用 Raw Source Snapshot 和 OCR/图片位置；
- Evidence Coverage、查询历史、拒绝原因和 Stop Reason；
- 内容去重结果以及预算消耗。

拒绝条件：把候选帖当证据、缺少原始来源、C 级内容进入证据集、OCR 尚未完成却声称完整。

### 4.2 Interview Preparation -> Tailored Resume / User

必须提交：

- 来源面试题与来源答案；
- 单独标记的模型补充答案；
- JD 重点、Candidate Background 证据缺口和学习计划；
- 所有面经结论的 evidence ID。

拒绝条件：补充答案伪装成来源答案、把“简历未体现”直接断言为“用户不会”、引用被拒绝证据。

### 4.3 Tailored Resume -> Application

必须提交：

- Tailored Resume 版本；
- 每一条经历、技能、项目和数字到 Candidate Background fact ID 的映射；
- 项目选择/删除理由和目标 JD 版本；
- 用户批准记录。

拒绝条件：出现无来源事实、基础简历被覆盖、JD 已变更、未获得用户确认。

### 4.4 Application -> Scheduling/Tracking

必须提交：目标 Job 版本、已批准简历版本、用户批准记录、平台回执或可验证失败原因。
没有正向平台回执不得标记 `SUBMITTED`。

### 4.5 Scheduling -> Journey

必须提交：时区化候选时间、free/busy 查询范围、HR 消息草稿、用户批准记录以及公司确认凭证。
HR 尚未确认时只能是 `PROPOSED`，不能写为 `CONFIRMED`。

## 5. 第一阶段落地

第一阶段不建设多 Agent，已经实现的最小状态骨架是：

1. SQLite 保存 Opportunity Journey、Task Run 和 Artifact Envelope；Artifact 保存校验状态与错误。
2. 文件系统保存 Raw Source Snapshot、图片、OCR、Markdown/JSON 大产物；SQLite 保存引用和哈希。
3. 单一 JobAgent 顺序执行 Research/OCR/Assessment/Preparation Task Run。
4. 快照文件完整性、Pydantic schema、哈希、A/B 准入、时效、卖资料风险与覆盖度由显式 Gate
   校验；阶段之间不靠聊天文本传数据。
5. 使用独立 SQLite LangGraph checkpointer，按 `thread_id` 跨进程恢复对话。

在拆分多 Agent 之前，再增加 Workstream、独立 Validation Report、HandoffManifest、幂等键、
assigned agent 和 Supervisor 调度；既有 Journey/TaskRun/Artifact 数据与业务 Tool 不迁移。
