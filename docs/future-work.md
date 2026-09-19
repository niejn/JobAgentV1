# Future Work

## Known Issue：SMTP 成功后的 Sent 文件夹确认存在延迟或缺失

XHS 邮件发送恢复优先通过发件邮箱的 Sent 文件夹验证 `Message-ID`，但 SMTP 成功并不保证
邮件已经立即出现在 Sent：

- 邮件可能尚未同步到 Sent；
- 部分 SMTP 服务不保存已发送邮件；
- IMAP 可能不可用、过期或暂时延迟；
- 本地 SMTP 客户端使用的 Sent 文件夹可能与服务器 Sent 不是同一个来源。

因此 `sending` 状态暂时查不到时只能标记为 `unverified` 并等待后续确认，不能自动重发，
避免 SMTP 已接受但本地没有及时看到 Sent 记录时产生重复投递。

## 高优先级：Langfuse 执行追踪、LLM 评估与 Prompt 管理

状态：已登记，待开发。按以下三个阶段顺序实施；覆盖主 DeepAgent 和各 Subagent。

### 第一阶段：Trace / Observation 接入

- 接入 Langfuse，记录每次大模型任务的 trace，以及模型调用、子 Agent 委派、Tool 执行等
  observation，保留父子调用关系。
- 关联会话 ID、Journey ID、任务 ID、Agent 名称、模型与供应商；记录输入输出、耗时、
  token 用量、可获得的费用、异常、重试和最终任务状态。
- 记录 HITL 暂停与恢复关系，区分等待用户审批的时间和实际执行耗时。
- 保留本地日志与 trace 的关联；Langfuse 不可用时不阻塞任务，服务退出时完成日志发送收尾。
- 验收：可从一次用户任务追踪到具体子 Agent、模型请求、工具结果、失败原因与投递回执。

### 第二阶段：LLM Eval 数据集与 Agent 任务评分

- 从已下载的岗位/帖子资料、历史任务和人工确认结果生成版本化评估数据集；每条数据包含
  任务输入、预期结果或评分标准、来源引用和任务类别。
- 覆盖岗位/JD/邮箱提取、查询改写、岗位匹配、邮件生成、工具选择及多 Agent 任务完成度。
- 结合确定性校验、人工评分和适用的模型评审；记录评审模型、评分规则版本及评分依据。
- 按 Agent、任务类型、模型、Prompt 版本查看任务评分、成功率、耗时、token 与成本，
  在同一数据集上比较候选模型，为各 Agent 选择合适的模型；保留独立验证集避免只优化样本。
- 离线评估复用本地快照与模拟外部工具，不因重放任务而向真实 HR 发消息或投递邮件。
- 验收：一次评估实验能复现结果，并能逐任务查看评分及失败案例，支持模型选择决策。

### 第三阶段：独立 Prompt 文件与 Langfuse Prompt 管理

- 将代码内的主 Agent、Subagent 和任务 Prompt 抽取为独立文件，明确模板变量与输入契约。
- 接入 Langfuse Prompt 管理，建立本地文件与远端版本的发布、版本选择、缓存及回退规则；
  明确各环境使用的版本，避免运行时静默漂移。
- 每次模型调用记录所用 Prompt 名称、版本和模型配置，使 trace、评估结果与 Prompt 变更关联。
- Prompt 更新先运行第二阶段评估，再按明确发布流程启用；支持回滚到已验证版本。
- 验收：可比较两个 Prompt 版本的任务评分，并能追溯线上调用使用的具体版本。

## XHS 邮件投递可靠性 P0：已实现（2026-09-10）

本轮完成：

- `BEGIN IMMEDIATE` 原子抢占同一 `draft_id`，提交 `sending` 和 Message-ID 后才执行 SMTP；
- `sending`、`unverified` 和 `submitted` 均禁止再次 SMTP，多进程共享同一 SQLite 状态；
- JobAgent 默认工具装配时扫描 `sending` / `unverified`，按 Message-ID 核验 Sent；
- Sent 精确命中后恢复 `submitted`；未命中、IMAP 不可用或旧记录缺少 ID 时保持 `unverified`；
- 独立保存 `sync_status`，启动和重复调用可补做登记册/Journey 同步，不重发已提交邮件；
- 缺失草稿的审批预览返回可读 `draft_not_found`，发送工具返回结构化失败；
- 补充真实子进程并发和崩溃、启动恢复、Sent 命中/未命中、旧表迁移、状态重放与交错竞态测试。

### 后续独立验收

- 真实 SMTP 正常发送已由用户手动验收；本轮 SMTP / IMAP 恢复测试全部使用离线替身。
- 真实邮箱的 Sent 文件夹名称、同步延迟和保留策略仍由实际邮箱环境决定。
- 更完整的 `StructuredTool` → 子 Agent `interrupt_on` → CLI `Command(resume)` 端到端验收
  属于后续渠道验收，不代表本轮已覆盖所有 CLI 交互路径。

## 待开发：FastAPI lifespan 管理 Boss CDP 调试 Chrome

将 Boss 专用调试 Chrome 的启动与关闭统一交给 FastAPI `lifespan` 管理，
浏览器生命周期与后端服务一致，避免每次执行任务都启动、关闭浏览器。

- 服务启动：检测配置的 CDP 端口；已有可用实例则复用，否则启动专用 Profile 的调试 Chrome，
  等待 CDP 就绪，记录进程归属和连接状态，并将共享连接管理器放入 `app.state`。
- 任务执行：通过共享管理器获取连接、借用/归还任务页面；任务结束不关闭 Chrome 进程。
- 服务关闭：停止接收新任务，等待在途任务结束或按超时策略取消，释放页面和 CDP 连接；
  仅关闭本服务启动的 Chrome 实例，不关闭用户预先启动或日常使用的 Chrome。
- 保留登录 Profile；连接中断时检查健康状态并重连，登录失效或风控时明确提示。
- 处理开发热重载和多 worker 的进程归属与端口竞争，避免重复启动或误关共享实例。
- 范围：后端运行时生命周期管理，不涉及网页前端功能；独立 CLI 的生命周期另行明确，
  不假设 CLI 会执行 FastAPI lifespan。

验收：连续执行多个 Boss 任务复用同一浏览器进程；服务退出能回收自建实例；
复用外部实例时只断开连接；启动失败、热重载及端口占用均有明确日志。

## 高优先级：Boss Subagent 会话预检 Middleware

在 `boss_recruiting` Subagent 内增加专用的 `BossSessionPreflightMiddleware`，不加载到 root、
XHS Subagent 或全局 Agent。它必须在 Boss Subagent 的任何工具执行前读取实时状态，禁止模型
仅凭历史对话中的“熔断截止时间”或“登录已恢复”做判断。

- 首次进入 Boss Subagent 时执行 `boss_session_status`：检查调试 Chrome/CDP、Boss 登录墙、
  验证码/风控、空白页和本地 Cookie 同步；状态不为 `ready` 时直接阻止所有 Boss 工具。
- `ready` 状态只在 middleware 进程内缓存 3–5 分钟；缓存不写入 checkpoint 或对话历史。
- 在 `awrap_model_call` 中可将状态作为本次请求的临时 system context 投影，但不能把动态状态
  永久追加到 `messages`，避免 reset 后模型继续复述旧熔断信息。
- 在 `awrap_tool_call` 中设置硬门，即使模型忽略临时上下文，也不能执行搜索、读取、建会话、
  打招呼、回复或简历发送。
- 遇到 `boss_login_required`、`code=7`、`page_lost`、`page_token_missing`、
  `boss_cookie_sync_failed` 时立即清除缓存；下一次 Boss 操作重新预检。`friend_add_rejected`
  属于岗位/平台业务拒绝，不能自动当作登录失效。
- `circuit_open` 必须每次从共享状态文件读取，不能由模型根据历史任务描述推导；reset 后下一次
  预检必须看到 `closed`。
- 预检只读，不创建会话、不发送消息、不触发 HITL；真正的 Boss 外部写 Tool 仍必须独立 HITL。

验收：reset 熔断后 Boss Subagent 不得继续报告旧截止时间；未登录时不会调用任何 Boss Tool；
用户登录并重新发起任务后只检查一次并同步 Cookie；同一缓存窗口内不会重复检查；缓存失效或
认证异常后会重新检查；root 和 XHS Subagent 的工具与行为不受影响。

## 高优先级：ResumeCraftingAgent（定制简历制作子 Agent）

新增不属于任何招聘渠道的 `resume_crafting` Subagent，使用 `resume-optimizer` 与 `kami` Skill，
将已确认 Candidate Background、基础简历和已选择的岗位 JD Evidence 制作为可追溯的 Tailored
Resume Artifact 与 PDF。它不接触 Boss、XHS、SMTP、Cookie 或任何外发 Tool；主 JobAgent 用原生
`task` 委派，渠道 Subagent 只消费已确认的简历 Artifact。

前置条件：将两个 Skill 安装为 JobAgent 运行时可读取的 `data/skills/*/SKILL.md`，并验证 Kami
需要的本地排版脚本、字体和 PDF 渲染环境。简历不得新增未经候选人确认的事实；每个 JD 要求都
应映射到简历事实或明确列为缺口。

## 低优先级：网页端招聘任务与投递回执看板

当前平台招聘 Subagent 的交互、HITL 批准和 `Command(resume=...)` 恢复仅由 CLI 承担。
网页前端不参与审批、不会持有 Agent `thread_id`，也不直接恢复被中断的 Agent。

未来在 Opportunity Journey 详情页增加只读看板：

- 展示 Boss/XHS 子 Agent 的任务进度、失败原因和最终状态；
- 展示已持久化的 `DeliveryReceipt`：渠道、收件人引用、岗位、简历/消息版本、确认状态和时间；
- CLI 正在等待审批时仅显示“等待 CLI 人工确认”，不提供网页批准按钮；
- 在 CLI 闭环稳定且有真实跨设备查看需求后，再评估网页审批与恢复是否值得单独设计。

该功能不属于当前 CLI 招聘闭环的验收范围。

## Future Work：XHS 作者联系、索要邮箱与内推

当招人帖没有可验证公开邮箱时，第一阶段停在 `contact_missing`，不自动评论、私信或联系作者。
未来可单独设计“联系作者”闭环：

- 只针对招聘帖作者或已验证招聘方账号，不能联系普通评论者；
- 生成索要官方投递邮箱或内推机会的简短消息草稿；
- 每条消息必须 CLI HITL，且有频率限制、去重、作者/帖子关联和回执；
- 作者回复后才可将其提供的邮箱升级为可审阅 Contact Channel；
- 不承诺内推，不把未回复或私信成功描述为已获得内推。

## 高优先级：平台招聘 Subagent 重构

在 Boss 主动发送简历功能完成后，将当前主 JobAgent 的平台底层能力按渠道收敛为两个
Subagent，主 JobAgent 只负责岗位匹配判断、Opportunity Journey 和跨渠道调度：

详细设计见 `docs/platform-subagent-rearchitecture-design.md`。

- `BossRecruitingAgent`：Boss 岗位发现、HR 会话、招呼、HR 回复后的简历发送和回执；
- `XhsRecruitingAgent`：小红书招人帖发现、JD/公开邮箱提取、定制邮件和附件简历投递；
- 主 Agent 不再直接持有 Boss token/会话、SMTP 或小红书抓取等平台私有工具；
- 两个 Subagent 交付 `JobDiscoveryResult`、短时 `ChannelActionDraft` 和最终
  `DeliveryReceipt`；外部写操作仍通过 HITL 向用户展示渠道、收件人、岗位和简历文件名。

## 高优先级：Boss HR 消息后台监控与受控自动回复

详细设计见 `docs/boss-hr-conversation-auto-reply-design.md`。其中补充了对 Hermes Agent
Gateway/Adapter/Watcher 架构的借鉴，以及 Boss 常驻监控 daemon 的生命周期设计。本节保留
交付优先级和范围摘要。

目标：JobAgent 常驻后台定时读取 Boss HR 新消息，识别需要候选人回应的问题，并在严格
事实边界内处理简单、低风险问答；复杂或未确认信息必须进入人工审批队列。

首批支持的问题类别：

- 期望薪资；
- 到岗时间；
- 在职/离职状态；
- 是否可到公司现场面试；
- 已确认的城市、工作方式和基础沟通安排。

流程：

```text
定时读取 Boss 会话增量消息
  -> 消息去重与 HR/岗位关联
  -> 意图分类、事实检索、置信度与风险判定
  -> 生成简短回复草稿
  -> 白名单事实 + 用户预先允许的自动回复策略：发送并记录回执
  -> 其他情况：写入待回复队列，展示 HR/岗位/原问题/草稿，等待 HITL 批准
  -> 发送后读取会话回执、审计并更新 Journey
```

安全与产品约束：

- 不从简历或历史对话猜测薪资、到岗日期、离职状态、面试可用时间；只能使用用户确认的
  `Job Search Profile`、候选人事实和显式回复策略；
- 默认不自动发送，先以草稿 + HITL 模式上线；用户可按问题类别、公司或 HR 明确开启
  自动回复；
- 涉及薪资谈判、offer、入职承诺、面试时间确认、个人联系方式、附件简历、拒绝/接受岗位
  或任何低置信度问题，永远要求人工批准；
- 按会话 ID + 来源消息 ID 幂等，失败/回执不明不得自动重试；
- 后台轮询必须使用已登录 Chrome 页面内接口、共享 Boss 限流和风控冷却，不做高频刷新。

建议实现切片：

1. ✅ `BossMessageMonitor`：常驻轮询、fencing 单实例租约、首次基线、会话变化探测、完整历史
   增量补偿和去重；
2. `HrQuestionClassifier` 和只读事实解析；
3. `HrReplyPolicy`（默认草稿、按类别/公司/HR 的自动回复授权）；
4. ✅ `BossReplyQueue` 与发送 worker：持久化待回复、审批、稳定会话寻址、原子发送抢占、
   回执和失败恢复；自动策略生产仍待第 2、3 项完成；
5. Agent/网页工具：查看待回复、批准草稿、修改后发送、配置自动回复策略；
6. 后台 worker 生命周期、限流、冷却、告警与可观测性。

管理 Channel 进度：CLI 与微信审批 Adapter、SQLite Outbox、主动微信通知以及统一
`jobagent watch --channel wechat --channel boss` 生命周期已实现；网页管理入口仍待开发。

## 已实现：损坏多模态 checkpoint session 的兼容处理

- 文本模型调用时只生成 request 级别的消息投影，不删除 checkpoint 中的原始图片。
- 模型明确返回不支持图片的 400 后，按“模型名 + Base URL”记录为 `text`，并清洗后重试一次。
- 悬空并发 tool call 补充取消结果并写回 checkpoint，不自动重放旧工具。
- `invalid_tool_calls` 和孤立 `ToolMessage` 在恢复时清理。
- 如果 checkpoint 本身无法反序列化，仍需要单独的数据库恢复工具。

## 小红书招人帖到 HR 邮件投递

状态：已登记，待实施。需求设计见
`docs/xhs-recruitment-lead-to-email-requirements.md`。

样本来源：

- [创业团队/Agent 招聘帖](https://www.xiaohongshu.com/discovery/item/6a82b8df0000000025014840)
- [Moodio 多岗位招聘帖](https://www.xiaohongshu.com/explore/6a2610dd000000001702d446)

目标流程：

```text
小红书招人帖
  -> 正文/图片/评论采集
  -> 招聘帖识别
  -> 图片 OCR/视觉解析
  -> 多岗位 JD 拆分
  -> 正文/图片/评论邮箱提取
  -> 招聘线索归档
  -> 岗位定制简历
  -> HR 自我介绍邮件草稿
  -> HITL 确认收件人/正文/附件
  -> SMTP 投递与回执审计
```

当前已有：

- `save_shared_url` 支持两类完整小红书笔记 URL；
- 可保存正文、原始响应和图片；
- `extract_shared_url` 可读取正文并执行图片 OCR；
- 用户提供的完整 URL 必须保留 `xsec_token`，不能使用裸 note_id。

待开发切片：

1. 评论抓取、快照归档和按评论 ID 去重；
2. 招聘帖分类器与不确定结果人工复核；
3. 正文/图片/评论中的 JD 证据提取；
4. 多岗位拆分为独立 Job Lead；
5. 投递邮箱提取、来源记录和可信度分级；
6. 第一阶段不生成定制简历：用户将已有 PDF 简历放入受控简历目录，Agent 仅列出可用 PDF
   文件名、大小和内容哈希；不得枚举目录外路径或猜测文件名；
7. 生成有岗位针对性的 HR 邮件草稿；
8. CLI 展示可用简历列表，用户明确选择一个文件名后，HITL 确认邮件收件人、正文和该附件；
9. SMTP 发送、幂等、防重复、Message-ID 和失败恢复；
10. Agent Tools：`find_recruitment_posts`、`extract_recruitment_leads`、
    `prepare_recruitment_email`、`send_recruitment_email`。

安全边界：不猜测邮箱、不把普通评论当官方 JD、不自动评论或私信、不把 SMTP 成功说成 HR 已读，
所有简历邮件外发都必须经过人工确认。

## 低优先级：Boss 打招呼按 HR 会话去重（发过消息的 HR 不再重复发送）

状态：**已实现（2026-09-19）**。发送前只读聊天列表预检（already_greeted_in_history，
覆盖绕过工具的发送盲区）+ 会话级 per-HR 检查（already_contacted_same_hr）+ 跳过即落库
（greeted + attempt submitted），下次运行走 already_contacted 快路径。CDP 旧传输路径
已整体删除（HTTP 直连是唯一路径，旧配置项被忽略）；真实环境整批验证待下次实际打招呼。

原始记录：
现象（2026-09-19 实测）：已发过消息的 HR 会再次收到打招呼。

根因（已核实）：

- `boss_conversations` 的唯一键是 `(job_id, friend_id)` 对
  （`jobagent/journey/boss_contact.py` 的 `UNIQUE(job_id, friend_id)`，
  docstring 明确"the (job_id, friend_id) pair, never the friend alone"）；
  `boss_greet_jobs` 的 `already_contacted` 检查按 job_id 走 `begin_attempt`，
  所以同一 HR 换一个岗位链接再 greet 时检查不命中，向同一会话再次发送；
- registry 之外的发送（Boss App/网页人工发送、agent 用 execute 手搓脚本直发）
  不留任何记录，现有检查完全不可见。

方案：

- 发送前按 `friend_id` 查询 `boss_conversations`：该 HR 已有会话时默认跳过，
  结果返回 `already_contacted_same_hr` 并附历史岗位（job_id/company/title），
  提示"如确要对另一岗位再次打招呼需用户显式确认"——不能粗暴全局禁发，
  对同公司不同岗位分别打招呼是合法求职行为，出口保留但默认不重发；
- 对 registry 盲区（App/脚本发送）：greet 批次前用只读
  `list_boss_greetings` 拉当前会话列表，按 friend_id / encrypt_boss_id 匹配
  已有会话（一次只读请求的成本），覆盖工具链外的历史接触；
- HITL 审批预览里显示"该 HR 已有会话（上次岗位 X）"，让用户在批准时就看到
  重复风险。

验收：同一 HR 第二个岗位 greet 默认跳过并说明原因；用户显式确认后可发；
仅经 App 手发过的 HR 也能被只读会话列表识别为已接触。
