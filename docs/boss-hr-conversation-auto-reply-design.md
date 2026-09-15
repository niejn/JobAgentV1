# Boss HR 对话扫描与受控自动回复设计

状态：daemon/adapter、持久化审批队列和发送 worker 已实现；LLM 分类和 CandidateFact
策略生成仍待开发。

已实现入口：

```bash
jobagent boss daemon
jobagent boss daemon --once
jobagent boss reply
jobagent boss reply-worker
jobagent boss reply-worker --once
jobagent watch --channel wechat --channel boss
```

当前 daemon 使用会话列表的 `lastMessage` 做低请求量变更探测，首次运行建立历史基线；后续
发生变化的会话通过稳定 `friend_id` 拉取历史增量，新入站消息写入
`boss_inbound_messages`。发送 worker 使用稳定的 `friend_id + friend_source + encrypt_boss_id`
定位会话，并要求 `approved` 或带有效策略凭证的 `auto_ready` 状态。发送采用 owner、generation
和到期 lease 原子抢占，在途任务持续续租；只恢复已过期发送。跨进程共享全局/单会话发送间隔
和每日上限，常驻 worker 额外执行 5–10 秒随机停顿。自动授权来自独立
`boss_reply_policies` 权威表，禁用、过期或版本不一致都会阻止发送。LLM 分类和 CandidateFact
策略生成仍由后续阶段实现。

### 管理 Channel（已实现）

CLI 与微信共用 `BossReplyApplicationService`，Channel 只解析输入和渲染结果，不直接操作
审批状态。Boss daemon 将新 HR 消息写入 SQLite Management Outbox；高风险草稿创建时也写入
同一个 Outbox。微信发送成功后确认 delivery，失败则保留并延迟重试。

微信 owner 可使用：

```text
/boss inbox
/boss list
/boss next
/boss approve <reply_id 前缀> <版本>
/boss edit <reply_id 前缀> <版本> <新正文>
/boss skip <reply_id 前缀> <版本>
```

审批命令必须包含唯一 reply ID 前缀和草稿版本；普通自由文本不会被解释为批准。微信 Channel
继续使用扫码 owner 白名单和最新 `context_token`，没有可用 token 时通知保留在 Outbox，CLI
仍可通过 `jobagent boss reply` 处理。

## 借鉴 Hermes Agent 的 Adapter / Gateway 设计

对 `D:\mashibing\hermes-agent` 的代码结构检查后，可以确认：Hermes 有一个常驻的
Gateway 进程（例如 `hermes gateway start`）。Gateway 负责平台 adapter 的连接生命周期、
入站事件路由、会话持久化、后台 watcher 和 cron；各平台 adapter 负责接收事件、发送消息和
平台协议细节。它不是由一个通用函数不断读取所有平台对话，而是由 adapter 的 webhook、长轮询
或 WebSocket 事件驱动；cron 是独立的定时任务调度器。

JobAgent 可以借鉴这种分层，但 Boss 需要一个专门的常驻监控 worker：

```text
JobAgent daemon / FastAPI lifespan
  -> BossConversationAdapter（Boss 协议、CDP/HTTP/WS、Cookie、限流）
  -> BossConversationMonitor（增量游标、退避、租约、去重）
  -> ConversationStore（消息、会话、岗位关联）
  -> HrReplyOrchestrator（分类、事实、草稿、策略）
  -> ReplyQueue（HITL / 自动回复）
```

`BossConversationAdapter` 只提供平台能力：`health()`、`list_conversations()`、
`list_messages(cursor)`、`send_reply()`、`verify_delivery()`。它不持有自动回复策略，也不直接
调用模型。这样将来替换 CDP、HTTP 或页面内 WebSocket 时，不会改变监控和审批逻辑。

`BossConversationMonitor` 是常驻后台任务，但必须是可停止、可恢复的 worker，而不是每次用户
对话临时创建一个子 Agent：

- 启动时读取上次成功游标，先建立连接和登录预检；
- 按固定最小间隔轮询，成功后使用短间隔，连续失败采用有上限的指数退避；
- 每轮只拉取增量消息，使用数据库租约保证多进程只有一个 active owner；
- 写入消息后立即释放平台连接，不等待模型生成或人工审批；
- 关闭时停止新轮询，等待当前读取结束，保留游标和待回复队列。

Hermes 的 Gateway 适合借鉴“adapter + 长生命周期 runner + watcher + 持久化状态”的结构，
但不能把 Boss 轮询塞进 cron：cron 适合离散任务，无法保证消息低延迟、游标连续性和单实例
租约。JobAgent CLI 可以启动同一个 monitor runner；FastAPI `lifespan` 则负责在服务模式下
启动和关闭它。

### 推荐的运行模式

默认采用 **daemon + 事件队列**：

1. `boss_monitor` 只读扫描并产生 `InboundMessageDetected` 事件；
2. `reply_orchestrator` 消费事件、分类并生成 `ReplyDraft`；
3. 需要人工确认的草稿进入持久化 `ReplyQueue`，不阻塞监控；
4. 用户批准后由独立 `reply_worker` 发送，发送结果写入 `DeliveryReceipt`；
5. `unverified` 只进入核实队列，不能直接重发。

短期也可以提供一次性 `jobagent boss monitor --once` 用于调试和手动同步，但不能把每次
用户问“检查 Boss 消息”都当作完整初始化/销毁一次监控器。

### reply_worker 如何与用户交互

`reply_worker` 不应直接弹出一个阻塞式窗口，也不应等待后台进程的 stdin。它只负责消费已经
批准的队列项；需要人工判断的消息写入持久化 `ReplyQueue`，状态为 `awaiting_human`。

推荐分两阶段：

**第一阶段：交互式 Boss 回复会话（优先实现）**

用户需要处理时启动专用入口：

```bash
jobagent boss reply
```

该命令不是通用 `jobagent chat`，只连接 ReplyQueue，进入一个批处理交互会话：

```text
daemon 发现 HR 新消息
  -> 写入 awaiting_human / auto_ready
  -> jobagent boss reply
       -> 汇总显示本批低风险自动处理数、高风险待确认数
       -> 低风险项显示草稿并按策略自动发送
       -> 高风险项逐条显示原文、事实、草稿和原因
       -> 用户选择 approve / edit / skip / quit
  -> reply_worker 发送并写回回执
```

一次会话默认处理一个批次（例如最多 20 条），高风险项目集中展示，用户不需要反复执行
`list/show/approve` 三个命令。用户退出后未处理项目仍保留在队列中；批准操作携带草稿版本，
防止批准旧草稿。

可以提供非交互参数供脚本使用：

```bash
jobagent boss reply --list
jobagent boss reply --approve <reply_id>
jobagent boss reply --quit
```

这些命令只处理 Boss ReplyQueue，不启动通用 Agent 对话。

### 风险分级与自动回复

每条草稿进入 `risk_assessment` 阶段，由 LLM 提取意图、引用事实并给出风险建议；随后由确定性
策略引擎复核。LLM 只能把问题标为“同级或更高风险”，不能绕过硬规则降级。

**低风险（满足事实已确认且用户开启该类别自动回复时可直接发送）**：

- 候选人明确可接受线上面试，只回复“可以先线上沟通”；
- 候选人明确不接受外包，只回复“不考虑外包岗位”；
- 候选人已有确认的到岗日期，只陈述该日期，不作额外承诺；
- 简短表达兴趣、确认继续沟通，不包含薪资、时间或入职承诺。

**高风险（永远进入交互式 HITL）**：

- 线下面试地点、现场面试方式或跨城安排；
- 预约具体日期、时间、时区或改期；
- 期望薪资、奖金、职级和谈判；
- 接受/拒绝外包、派遣、第三方用工，或对已知用工形式作出谈判承诺；
- 到岗日期未确认、需要承诺“随时到岗”或涉及离职安排；
- 发送简历、联系方式、证件或其他附件；
- LLM 置信度不足、消息含多个问题或上下文冲突。

用户提出的“到岗时间”和“不接受外包”只有在候选人事实已明确、回复内容只是复述事实、
且用户开启该类别自动回复时才属于低风险；否则仍然转为高风险。对于“是外包职位吗？”这类
问题，系统可以直接询问 HR：“请问该岗位是贵司自有编制，还是外包/派遣用工？劳动合同签署
主体和汇报关系分别是什么？”这属于事实澄清，不代表候选人接受该安排。收到 HR 的回答后，
如果下一步需要表达接受或拒绝，再进入 HITL。

风险评估输出必须包含：`intent`、`risk_level`、`confidence`、`fact_ids`、`blocked_reasons`、
`policy_version`。缺少 `fact_ids` 或命中硬规则时，状态只能是 `awaiting_human`。

### 交互式批处理体验

进入 `jobagent boss reply` 后先显示摘要：

```text
Boss 待回复：7 条
低风险自动回复：3 条（等待发送）
高风险待确认：4 条

[1/4] 上海智诚云链 / 大模型后端研发 / HR 王女士
HR：您好，我们约一下线下面试时间，您最近什么时间方便？
风险：high · interview_time · 需要确认具体日期和时间
草稿：我对岗位很感兴趣，也可以先线上沟通。具体时间我确认后回复您。
操作：a=批准  e=编辑  s=跳过  q=退出
```

低风险项也应在批次摘要中可见，并显示发送结果；默认可以设置为“先展示后自动发送”，便于
用户观察策略是否符合预期。自动发送失败或回执不明时，不阻塞后续审批，转入 `unverified`。

**第二阶段：网页/桌面审批**

如果需要后台常驻而用户不保持终端，可以提供本地 Web UI 或系统通知：

- Web UI 展示 HR 原文、岗位、事实来源、草稿和风险原因；
- “批准发送”请求回到同一个 daemon，由 daemon 校验用户身份、草稿版本和幂等键；
- 系统通知只负责提醒，点击后打开审批页面，不能把完整敏感内容放进通知正文；
- 没有网络或 UI 时，CLI 仍是后备入口。

不建议把原生弹窗作为唯一入口：后台进程可能运行在无桌面环境、远程服务器或多个用户会话
中，弹窗也无法可靠表达审批超时、版本冲突和发送回执。若未来需要桌面体验，应让弹窗成为
CLI/Web 队列的通知层，而不是另一套审批状态。

### HITL 状态与超时

- `awaiting_human` 不设置自动发送超时；超过配置期限后标记 `expired`，不丢弃原始消息。
- 用户批准时重新检查 Boss 登录、风控、会话和草稿版本；检查失败不消耗批准，回到可重试状态。
- 用户批准后立即转为 `approved` 并由 worker 原子抢占为 `sending`，同一 `reply_id` 只能有一个发送者。
- CLI/Web 重复点击只返回已有状态，不产生第二条外发消息。

### 生命周期和故障边界

- daemon 只管理 JobAgent 创建的 Boss CDP/WS 连接；连接断开时重连，不关闭用户的其他浏览器。
- 登录失效、风控和页面丢失属于 adapter 健康状态；monitor 进入退避并通知用户，不生成回复。
- monitor 崩溃由 runner supervisor 重启；游标和消息去重状态已经持久化，不重复触发草稿。
- reply worker 与 monitor 分离，发送阻塞或 HITL 等待不会停止新消息扫描。
- 多进程部署使用文件/SQLite lease；只有 lease owner 轮询 Boss，其他进程读取队列。

## 目标

JobAgent 能扫描已经存在的 Boss HR 会话，识别 HR 的新消息，并在候选人事实明确、回复风险可控时生成回复。典型场景包括：

> 您好，我们约一下线下面试时间，您最近什么时间方便？

以及 HR 在收到打招呼后询问是否外包、能否线上面试、期望薪资、到岗时间、在职状态等问题。

系统必须区分“已识别问题”“已生成草稿”“已发送”和“已确认送达”，不能因为模型生成了答案就认为消息已经发出。

## 核心领域对象

- **Conversation**：Boss HR 会话，与岗位、公司、HR 身份关联。
- **InboundMessage**：HR 发来的原始消息，使用平台消息 ID 去重。
- **HrIntent**：从一条或连续几条 HR 消息识别出的意图，例如 `interview_mode`、`interview_time`、`outsourcing`、`salary_expectation`、`availability`、`employment_status`。
- **CandidateFact**：候选人已经确认的事实，例如期望薪资区间、最早到岗日期、可面试时间、是否接受外包、可接受工作方式。
- **ReplyDraft**：针对一条或一组入站消息生成的候选回复，包含事实引用、置信度和风险等级。
- **ReplyAuthorization**：用户对回复策略的授权，按问题类别、公司/HR、会话和有效期限定。
- **ReplyAttempt**：一次发送尝试，使用 `conversation_id + source_message_ids + draft_version` 做幂等键。
- **DeliveryReceipt**：发送回执，状态为 `submitted`、`unverified` 或 `failed`。

## 扫描流程

```text
读取已登录 Boss 会话列表
  -> 按会话 ID 增量读取消息
  -> 用 platform_message_id 去重并持久化
  -> 只处理 HR 发来的、尚未处理的消息
  -> 关联岗位、公司和 HR
  -> 意图识别与事实检索
  -> 生成回复草稿和风险判定
  -> 自动发送或进入 HITL 待回复队列
  -> 发送后核实历史消息并更新 Journey
```

扫描使用现有登录 Chrome 页面内接口或已经建立的 Boss 会话能力，遵守共享限流和风控冷却。扫描失败不能被解释为“没有新消息”。

增量游标按会话保存；首次扫描可以建立历史基线，默认不对历史消息自动回复，只处理基线之后的新消息。

## 意图与处理策略

| 意图 | 示例 | 默认处理 | 允许自动发送的条件 |
|---|---|---|---|
| `interview_mode` | 能否先线上面试 | 生成草稿并 HITL | 用户预先确认接受线上面试，且只表达意愿，不承诺时间 |
| `interview_time` | 最近什么时间方便 | HITL | 永远需要确认具体日期和时间 |
| `outsourcing` | 是外包职位吗 | 自动澄清 | 只向 HR 询问用工主体、劳动合同和汇报关系，不代表候选人接受或拒绝 |
| `salary_expectation` | 期望薪资多少 | HITL | 默认禁止自动发送；薪资谈判必须人工确认 |
| `availability` | 什么时候能到岗 | HITL | 用户已确认明确日期且授权该类自动回复时才可自动发送 |
| `employment_status` | 目前在职吗 | HITL | 用户已确认当前状态且授权该类自动回复时才可自动发送 |
| `basic_interest` | 对岗位感兴趣吗 | 草稿 | 可在用户开启低风险自动回复后发送 |
| `unknown` / `multi_intent` | 含糊、多个问题或上下文不足 | HITL | 不允许自动发送 |

## 候选人事实边界

事实只能来自：

1. 用户明确确认的求职档案；
2. 用户在当前会话中明确确认的信息；
3. 已确认的岗位/公司偏好和有效期内的回复策略。

不能从简历措辞、旧聊天、模型记忆或岗位描述推断薪资、到岗日期、离职状态、是否接受外包或具体面试时间。事实过期、冲突或缺失时，必须进入 HITL。

## 回复示例

对“您好，我们约一下线下面试时间，您最近什么时间方便？”：

- 若没有已确认时间：生成“我对岗位很感兴趣，也可以先线上沟通。我的时间需要确认后回复您，您这周有哪些时间段方便？”并进入 HITL；
- 若用户确认了时间：草稿中明确列出日期、时区和方式，仍需用户批准后发送；
- 不得直接承诺“明天下午”“随时可以”这类未经确认的信息。

对“是外包职位吗？”：

- 若系统没有该岗位的可靠招聘主体信息：可以自动向 HR 询问岗位的用工主体、合同签署主体和汇报关系；
- 不得依据公司规模、岗位描述或 HR 名称猜测是否外包。

对“期望薪资多少？”：

- 默认只生成草稿，不自动发送；
- 草稿必须展示使用的已确认薪资区间、月薪/年薪口径和是否包含奖金；
- 用户可以修改后再批准发送。

## HITL 设计

HITL 卡片至少展示：

- 公司、岗位、HR 和会话；
- HR 原文及消息时间；
- 识别出的意图和置信度；
- 使用的 CandidateFact 及其确认时间；
- 回复草稿；
- 风险等级和需要人工确认的原因；
- 发送后回执策略。

审批选项：批准发送、编辑后发送、忽略本条、为该类别建立临时授权。授权必须有范围和有效期，不能因为批准一次薪资回复就永久允许所有薪资回复。

## 状态机

```text
new
  -> classified
  -> drafted
  -> awaiting_human
  -> approved
  -> sending
  -> submitted | unverified | failed
```

`submitted` 只表示已取得协议或历史确认；`unverified` 表示发送结果不确定，禁止自动重发；`failed` 只用于明确未发布、平台拒绝或参数错误。任何状态都必须保留原始 HR 消息和审计记录。

## 幂等与安全规则

- 入站消息按 `platform_message_id` 去重；没有平台 ID 时使用会话、时间、文本哈希生成临时指纹，并标记低可信度。
- 出站回复按 `conversation_id + source_message_ids + draft_version` 幂等。
- `sending`、`unverified` 状态禁止再次自动发送，必须先读取会话历史核实。
- WebSocket 正常关闭、ACK 超时或连接断开都先核实历史，不能直接判定失败。
- 任何自动回复只允许发送基于已确认事实的短文本；不得自动发送简历、联系方式、承诺入职或接受/拒绝岗位。
- 会话扫描和回复发送共享 Boss 登录预检、限流和熔断；扫描只读失败不能触发写操作。

## 分阶段交付建议

1. 只读扫描：会话增量、消息归档、去重和岗位关联。
2. 意图与事实：分类器、事实检索、置信度和未知处理。
3. 草稿队列：CLI 查看、编辑、批准和审计。
4. 受控自动回复：只开放低风险类别和显式授权，默认仍走 HITL。
5. 回执与 Journey：发送历史核实、`unverified` 恢复和跟进状态同步。

## 验收标准

- 首次扫描不会对历史 HR 消息自动回复。
- 同一 HR 消息重复扫描只生成一条待处理记录。
- “线下面试时间”“薪资”“外包”“到岗时间”等问题不会在缺少事实时自动发送。
- 用户批准后只发送对应草稿，审批前不产生外发动作。
- ACK 丢失但消息实际存在时，最终状态为 `submitted` 或 `unverified`，不会重复发送。
- 扫描、草稿、审批、发送和回执都能关联到会话、岗位和 Journey。
