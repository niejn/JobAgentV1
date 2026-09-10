# Boss 职位联系与简历投递需求设计

> 状态：职位到 HR 直接联系闭环已实施（2026-09-03）
> 版本：v1.0（2026-09-03）
> 范围：Boss 职位发现、立即沟通、新建 HR 会话、招呼、简历请求审批与简历发送

## 1. 产品目标

JobAgent 能从用户确认的求职画像中发现 Boss 岗位，筛选出值得联系的岗位，并在
用户授权范围内完成：

```text
岗位发现 -> 岗位确认 -> 立即沟通 -> HR 会话建立 -> 定制招呼
  -> 简历准备/选择 -> 简历请求审批或主动发送 -> 多重回执 -> 状态归档
```

系统必须区分“平台动作已提交”和“平台已确认完成”。任何没有正向回执的动作都只能是
`unverified`，不能写成已联系或已投递。

## 2. 边界与默认策略

### 2.1 默认模式

- 搜索、读取岗位、读取 HR 消息可以自动执行。
- 新建 HR 会话、发送招呼、发送简历、同意 HR 的简历请求都是外部写操作。
- 默认每次外部写操作都经过 HITL；模型不能通过参数自行绕过。
- 用户可显式开启“策略自动审批”，但必须限定公司、职位、简历版本和风险条件。

### 2.2 明确非目标

- 不绕过验证码、设备校验、风控或平台频率限制。
- 不自动发送未经用户确认的简历版本。
- 不因“立即沟通”按钮点击成功就推断会话建立成功。
- 不因 MQTT PUBACK 单独存在就推断 HR 已收到简历。
- 不做无人监管的批量外联；单次运行必须有数量、时间和失败熔断上限。

## 3. 核心领域对象

### 3.1 Job Posting

Boss 展示的一版岗位信息。包含 `platform_job_id`、公司、职位、地点、JD、岗位 URL、
招聘者信息和抓取时间。

### 3.2 HR Conversation

Job Posting 与某位招聘者之间的 Boss 会话。会话可能在“立即沟通”后才创建，必须保存
`friend_id`、`encrypt_boss_id`、`friend_source`、`security_id` 和首次发现时间。

### 3.3 Contact Attempt

一次针对岗位的外联尝试。它不是岗位状态，也不是消息本身；它记录目标、动作、授权、
幂等键、传输结果和最终确认结果。

### 3.4 Resume Request

HR 在会话中发出的请求候选人提供简历的卡片。当前已识别为 Boss Protobuf 消息中的
`body.type = 9`，必须使用来源消息 `mid` 作为幂等键。

### 3.5 Resume Delivery

某个具体简历版本向某个 HR 会话发送的一次投递。它必须关联 `resume_artifact_id`、
`resume_sha256`、`conversation_id` 和发送授权，不能只记录“已发简历”字符串。

### 3.6 Delivery Receipt

证明平台动作结果的回执。按可信度分层：MQTT PUBACK < 会话历史中的对应消息 <
平台附件/简历同步状态。只有满足规定的最小回执组合才能进入 `confirmed`。

## 4. 用户故事

- 用户希望 Agent 每周搜索符合画像的 Boss 岗位，并展示公司、职位、薪资、地点、JD 摘要和匹配依据。
- 用户确认岗位后，希望 Agent 点击“立即沟通”，建立对应 HR 会话并发送岗位定制招呼。
- HR 请求简历时，用户希望系统识别请求卡片，并使用指定的已确认简历版本处理。
- 用户开启自动审批策略后，希望满足规则的简历请求自动同意；超出规则时必须暂停。
- 用户希望每个岗位都能回答：是否联系过、使用了什么文案、发送了哪一版简历、平台是否确认。

## 5. 端到端工作流

### 5.1 岗位发现

1. Agent 读取已确认的 Job Search Profile 和 Candidate Background。
2. 调用只读 Boss 岗位发现工具。
3. 按 Job Identity 去重，执行硬约束过滤和匹配排序。
4. 保存 Job Posting、JD 版本和 Recommendation Run。
5. 输出待用户确认的岗位清单，不执行外部写操作。

### 5.2 立即沟通与会话创建

1. 用户确认具体岗位和招呼内容。
2. 系统检查 Application Eligibility、冷却时间和已有会话。
3. 若会话不存在，执行岗位卡片的“立即沟通”动作。
4. 通过 `friend_id/encrypt_boss_id` 或历史列表确认 HR Conversation 已创建。
5. 会话创建失败时停止，不发送后续消息。
6. 会话已存在时不得重复创建或重复打招呼。

当前实现：`BossHttpBackend` 直接搜索并保留内部 `securityId/lid/encryptBossId`；
`BossDirectContactAdapter` 调用
`POST /wapi/zpgeek/friend/add.json?securityId=&jobId=&lid=`，表单为 `expectId=0`，
并通过会话列表确认 `friend_id`。`geekEnter` 仅用于进入已有会话，不作为创建成功证据。
默认组合为 `BOSS_SEARCH_TRANSPORT=cdp` 与 `BOSS_CONTACT_TRANSPORT=http`：岗位发现
继续复用已登录 Chrome 的 CDP，`boss_greet_jobs` 则不依赖职位网页，直接创建会话并在确认后
通过 MQTT/WS 发送定制招呼。仅在明确设置 `BOSS_CONTACT_TRANSPORT=cdp` 时，才会走页面
“立即沟通”按钮路径。

首次 Boss 操作若本地 CDP 端口尚未就绪，JobAgent 启动专用、可见的 Chrome profile 并打开
Boss 页面。系统先检查登录态；未登录则明确提示用户在该 Chrome 手动登录。用户完成后回复
“已登录”并重新发起 Boss 操作，系统重新验证页面、同步 `.zhipin.com` Cookie 到本地，再允许
搜索和直连外联。`jobagent login --platform boss` 也执行同一验证和同步流程：已有调试 Chrome
时复用，端口未就绪时启动专用 profile；不会关闭既有 Chrome，也不会发送消息。

若独立 HTTP 会话无法从用户信息响应取得短期 page token，直连模块会只读连接已登录 Chrome，
从现有 Boss 页面或一个短生命周期聊天页读取运行时 token；token 只在当前内存流程中传给
`friend/add` 和 WebSocket 凭证请求，不写入配置、日志或数据库。读取失败返回
`page_token_missing`，不自动改走职位页 UI。

### 5.3 招呼发送

1. 生成基于 JD 和候选人事实的定制招呼。
2. 记录待发送文本的哈希和版本。
3. HITL 或策略授权通过后发送。
4. 等待 MQTT PUBACK，并读取会话历史确认文本出现。
5. 成功后写入 Contact Attempt 和 `conversation_messages`。

### 5.4 HR 请求简历

1. WS Listener 解码入站 Protobuf 消息。
2. 识别 `body.type = 9` 或等价的简历请求结构。
3. 以 `conversation_id + request_mid` 去重并写入 `resume_requests`。
4. 选择规则匹配的 Tailored Resume；没有已确认简历时进入 `needs_user_input`。
5. 默认暂停等待用户确认；策略自动审批时执行规则检查。
6. 发送同意动作或调用对应平台动作接口。
7. 发送后等待 PUBACK、会话历史和简历同步状态。
8. 任一关键回执缺失，状态为 `unverified`，不得自动重试。

## 6. 状态模型

### 6.1 HR Conversation

```text
UNKNOWN -> CREATING -> ACTIVE
                    \-> CREATE_FAILED
ACTIVE -> CLOSED -> ACTIVE
```

`ACTIVE` 只表示平台确认存在会话，不表示已经发送过消息。

### 6.2 Contact Attempt

```text
DRAFT
  -> WAITING_APPROVAL
  -> AUTHORIZED
  -> CREATING_CONVERSATION
  -> CONVERSATION_CONFIRMED
  -> SENDING_GREETING
  -> GREETING_CONFIRMED
  -> FAILED | UNVERIFIED | CANCELLED
```

### 6.3 Resume Request

```text
RECEIVED
  -> DEDUPLICATED
  -> WAITING_APPROVAL
  -> APPROVED
  -> ACCEPT_ACTION_SENT
  -> RESUME_DELIVERY_PENDING
  -> CONFIRMED
  -> REJECTED | EXPIRED | UNVERIFIED | FAILED
```

### 6.4 Resume Delivery

```text
PREPARED -> AUTHORIZED -> PUBLISHING -> PUBLISHED
                                   \-> UNVERIFIED
PUBLISHED -> HISTORY_CONFIRMED -> ATTACHMENT_CONFIRMED
```

## 7. 数据模型

```sql
CREATE TABLE hr_conversations (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    friend_id INTEGER NOT NULL,
    encrypt_boss_id TEXT NOT NULL,
    friend_source INTEGER NOT NULL,
    security_id TEXT,
    job_identity TEXT,
    status TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_message_at INTEGER,
    UNIQUE(platform, friend_id)
);

CREATE TABLE resume_requests (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_mid INTEGER NOT NULL,
    card_type INTEGER NOT NULL,
    request_payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    selected_resume_id TEXT,
    received_at INTEGER NOT NULL,
    expires_at INTEGER,
    UNIQUE(conversation_id, source_mid)
);

CREATE TABLE contact_attempts (
    id TEXT PRIMARY KEY,
    job_identity TEXT NOT NULL,
    conversation_id TEXT,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    authorization_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    request_hash TEXT,
    transport_receipt_json TEXT,
    confirmed_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE resume_deliveries (
    id TEXT PRIMARY KEY,
    request_id TEXT,
    conversation_id TEXT NOT NULL,
    resume_artifact_id TEXT NOT NULL,
    resume_sha256 TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    puback_at INTEGER,
    history_confirmed_at INTEGER,
    attachment_confirmed_at INTEGER,
    error_type TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE automation_audit_events (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
```

凭证、Cookie、WS password、完整简历正文不进入审计表；只保存哈希、版本和引用。

## 8. 自动审批策略

策略必须是显式配置，且在执行前生成可解释的决定：

```yaml
resume_auto_approval:
  enabled: false
  allowed_companies: []
  allowed_job_identities: []
  allowed_resume_ids: []
  max_daily_approvals: 3
  require_hr_request: true
  require_active_conversation: true
  reject_if_resume_changed: true
  reject_if_request_age_minutes: 1440
```

以下任一条件命中，必须转人工：目标不唯一、简历版本未确认、岗位身份变化、请求过期、
当日额度耗尽、平台回执异常、连续风控错误或同一请求已有处理记录。

## 9. 幂等与失败恢复

- 创建会话：`platform + platform_job_id + friend_id`。
- 发送招呼：`conversation_id + message_hash + action_kind`。
- 处理简历请求：`conversation_id + source_mid`。
- 发送简历：`conversation_id + resume_sha256 + request_id`。
- 任何超时都进入 `UNVERIFIED`，不自动重发；恢复任务只读取历史和状态，不猜测结果。
- 进程重启后从数据库恢复 `RECEIVED/WAITING_APPROVAL/PUBLISHING` 任务。
- 连续 `page_blocked`、`auth_failed` 或协议错误触发 Boss 熔断器。

## 10. 验收标准

### 岗位到 HR

- 能发现并去重岗位，展示匹配依据。
- 用户确认后能识别“立即沟通”并确认会话已建立。
- 会话创建失败时不发送招呼或简历。
- 已有会话不会重复创建或重复发送。

### 简历请求

- 能识别简历请求卡片并展示 HR、公司、岗位、请求时间和将使用的简历版本。
- 默认模式未经确认不执行同意动作。
- 自动审批只对策略明确允许的请求生效。
- 同一请求重复收到时只处理一次。
- 简历发送必须关联版本、哈希和审计记录。

### 成功确认

- PUBACK 缺失：`unverified`。
- 有 PUBACK、无历史消息：`unverified`。
- 有历史消息、无简历同步确认：`history_confirmed`，不能标记最终完成。
- 三重回执满足：`confirmed`。
- 失败不得自动重试，必须给出可执行的人工处理建议。

### 安全

- 默认 HITL 开启，模型不能改变授权模式。
- 所有外部文本经过 Prompt 注入检测后才能进入 Agent 上下文。
- 日志和审计不包含 Cookie、token、WS password 或完整简历正文。
- 单元测试使用 Fake Boss transport；真机测试只使用隔离小号。

## 11. 实施切片

1. `BossWsTransport`：MQTT 分包、protobuf 解码、入站消息事件和 ACK 分类。
2. `HR Conversation Registry`：会话身份、岗位关联和幂等。
3. `BossMessageListener`：监听、解码、简历请求识别和断点恢复。
4. `ResumeApprovalQueue`：队列、策略评估、HITL 和过期处理。
5. `ResumeActionSender`：同意/拒绝动作、请求级幂等和回执等待。
6. `ResumeDeliveryVerifier`：PUBACK、历史消息、简历同步三层确认。
7. `discover -> contact -> greeting -> resume` 编排工具和 Agent prompt。
8. 熔断、审计、CLI 状态查询、真机小号验收。

## 12. 完成定义

给定一个已确认的 Boss 岗位和已确认简历，JobAgent 能够在策略允许时：发现岗位、创建唯一
HR 会话、发送一次定制招呼、识别 HR 简历请求、选择正确简历、完成一次幂等审批，并以可追溯
的多重平台回执确认结果；任意异常都不会重复发送或伪造成功状态。
