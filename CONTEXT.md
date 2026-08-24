# JobAgent 求职旅程

JobAgent 围绕求职者针对具体岗位的完整旅程组织信息、决策与行动，并将高风险外部操作保留给人工确认。

## Language

**JobAgent（求职智能体）**:
面向单一使用者、围绕 Opportunity Journey 进行对话、补齐缺失信息、选择业务 Tool 并协调
人工确认的系统入口；用户不直接操作爬虫、下载器或平台传输参数。
_Avoid_: CLI 命令集合、爬虫脚本、无人监管自动投递器

**Opportunity Journey（岗位求职旅程）**:
求职者针对某家公司一个具体岗位，从岗位发现、面试调研和准备，到内推、投递、面试、结果追踪与复盘的完整生命周期；它是系统的核心工作流边界。
_Avoid_: 单次投递、岗位任务、超级 Agent

**Interview Preparation Pack（面试准备包）**:
针对某个 Opportunity Journey 汇总的可追溯面经证据、常见问题、岗位能力矩阵、候选人薄弱点、学习计划与模拟面试脚本；第一阶段只生成准备包，不执行实时模拟面试。
_Avoid_: 模拟面试、通用题库、面经摘要

**Interview Evidence（面经证据）**:
可归因到目标公司的真实面试经历资料，只允许 A 级（同公司、同岗位）和 B 级（同公司、相近岗位或同职能）；其他公司的同类岗位资料不得作为降级证据进入准备包。
_Avoid_: 通用面经、跨公司同岗经验、C 级证据

**Interview Search Plan（面经检索计划）**:
面经研究循环在某一轮准备执行的结构化搜索动作，记录本轮查询、过滤条件、目标证据缺口和
预算；它会根据上一轮实际取得的帖子质量与覆盖度持续修订，不是整次研究的一次性静态答案。
_Avoid_: 单一搜索词、完整研究流程、面经证据、一次性通用关键词列表

**Interview Evidence Research Loop（面经证据研究循环）**:
针对一个具体 Job/JD 反复执行“分析证据缺口、规划下一轮搜索、采集并识别原始内容、评估
质量与覆盖度、决定继续或停止”的自适应研究过程；只把满足 A/B 准入规则的资料作为面经证据。
_Avoid_: 一次性 Query Planner、无限搜索、固定关键词批处理

**Evidence Coverage（证据覆盖度）**:
当前已准入面经证据对目标岗位关键维度的覆盖情况，包括公司与岗位匹配、面试轮次、岗位能力
或技术主题、新鲜度及独立来源数量；数量充足但内容重复不代表覆盖充分。
_Avoid_: 搜索结果数量、下载数量、LLM 主观满意度

**Post Relevance Assessment（帖子相关性判定）**:
在取得帖子正文和图片提取内容后，对候选帖子与目标 Job/JD 的公司、岗位、技术栈、真实面试
信号、新鲜度及商业风险进行的可解释结构化判定；它决定原始快照是否能升级为 A/B 级面经证据。
_Avoid_: 搜索排序、关键词命中、原始帖子

**Raw Source Snapshot（来源原始快照）**:
从面经来源实际取得且不可变更的原始帖子数据，保留来源标识、URL、正文、作者与时间元数据、抓取时间、内容哈希及按帖子顺序下载的原始图片；标准化证据和准备包都必须能追溯到它。第一阶段不包含评论、私信或聊天记录。
_Avoid_: 面经摘要、LLM 提取结果、准备包

**Reusable Source Corpus（可复用来源语料库）**:
跨 Opportunity Journey 持久保存的 Raw Source Snapshot 集合；同一来源帖子可以针对不同 Job/JD
重新执行 Post Relevance Assessment，而不必重复下载正文、图片或 OCR。它是原始资料库，不是可以
随时丢弃的临时缓存；后续向量数据库、全文索引或图谱都是可重建的检索索引，不能替代它。
_Avoid_: 当前岗位证据集、Post Relevance Assessment、向量数据库、临时下载目录

**Source Image（来源图片）**:
属于 Raw Source Snapshot 的原始图片资产，保存帖子内顺序、来源 URL、MIME、尺寸、内容哈希和本地路径；OCR 或多模态识别文本不是图片本身，而是可重新生成的派生结果。
_Avoid_: OCR 文本、视频、外链缩略图

**Image Content Extraction（图片内容提取）**:
使用本地 OCR 或多模态模型从 Source Image 提取文字、结构和上下文的过程，结果必须记录引擎、模型或版本、置信度并引用原始图片。
_Avoid_: 原始图片、人工确认文本

**Source Author Reference（来源作者引用）**:
面经来源平台中的最小作者标识，只保存平台、用户名或显示名、作者 ID，以及作者与其帖子之间的关系；第一阶段不保存头像、私信或聊天记录。
_Avoid_: 联系人画像、内推联系人、社交关系档案

**Derived Preparation Artifact（派生准备产物）**:
由一个或多个 Raw Source Snapshot 经标准化、筛选和生成得到的 Markdown/JSON 准备材料；它可以重新生成，不能覆盖或替代原始快照。
_Avoid_: 原始帖子、唯一事实源

**Evidence Gap Report（证据缺口报告）**:
当目标公司不存在可用 A/B 面经证据时生成的明确声明，记录检索范围和缺失结果，防止系统把推断内容表述为真实面经。
_Avoid_: 空面经、搜索失败

**JD Preparation Pack（JD 准备包）**:
在缺少 A/B 面经证据时，仅依据目标公司的官方 JD、技术栈和公开招聘资料生成的准备材料；其中所有预测内容都必须标记为“基于 JD 推断”。
_Avoid_: 面试准备包、真实面试题

**Job Search Profile（求职画像）**:
当前使用者全局唯一、经其确认的长期求职目标与约束，描述期望职位、薪资、地点、公司/职位
特征和不可接受条件；它不描述候选人经历，也不随阶段性投递策略被静默改写。
_Avoid_: 简历、候选人背景、岗位画像

**Company Feature（公司特征）**:
用于描述候选雇主的可比较属性，包括所有制/地域属性、行业、发展阶段、规模层级、业务模式和
工程文化；“大中小厂”只是其中一个维度。
_Avoid_: 公司名称、求职画像、公司好坏

**Job Feature（职位特征）**:
用于描述具体岗位工作内容与条件的可比较属性，包括职能、职级、技术方向、职责边界、业务线、
成长路径、工作方式和风险要求。
_Avoid_: JD 原文、Candidate Background、岗位匹配分数

**Search Strategy Portfolio（求职策略组合）**:
在一段时间内对热身、主目标和冲刺岗位的比例、筛选重点与预算分配；它可随投递和面试反馈调整，
但不能覆盖 Job Search Profile 中用户确认的硬约束。
_Avoid_: 求职画像、单一求职阶段、自动投递计划

**Strategy Revision（策略修订）**:
由最新投递、面试进展、能力差距和用户反馈触发的 Search Strategy Portfolio 新版本，保留原策略、
调整依据和生效时间。
_Avoid_: 静默偏好漂移、覆盖求职画像、一次搜索改词

**Application Route（投递路径）**:
一个具体 Opportunity Journey 采用的岗位触达与投递方式，例如 Boss 直投、官网 ATS 或内推；它由
当前策略、公司/职位特征和渠道可用性决定，并独立于面经研究来源。
_Avoid_: 求职策略组合、面经来源、自动投递授权

**Job Posting（岗位发布）**:
某个招聘来源在特定时间展示的一版岗位信息，包含来源、外部岗位 ID、URL、JD 和发布时间；同一岗位可以有多个来源或历史版本。
_Avoid_: Opportunity Journey、投递记录、唯一岗位

**Job Identity（岗位身份）**:
用于判断多个 Job Posting 是否描述同一个实际岗位的稳定身份；第一阶段以规范化公司与规范化职位组成，来源和 JD 版本变化不创建新身份。
_Avoid_: URL、帖子 ID、Opportunity Journey

**Job Recommendation Run（岗位推荐运行）**:
按一次明确时间窗口执行的岗位发现、去重、匹配、排序和推荐记录；每周运行必须能说明新增、重复、过滤和推荐了哪些岗位。
_Avoid_: 一次搜索请求、Opportunity Journey、自动投递

**Application Eligibility（投递资格）**:
在创建或执行投递前，对岗位是否允许进入投递流程的结构化决定；它综合历史投递、同公司多岗位策略、冷却期和人工覆盖，不等同于岗位匹配度。
_Avoid_: 匹配分数、去重结果、投递 Agent 的临时判断

**Candidate Background（候选人背景）**:
描述使用者已经具备的教育、工作经历、项目、技能和可证明成果的结构化事实，通常从简历解析并经用户确认。
_Avoid_: 求职画像、求职偏好、目标岗位

**Resume（简历）**:
承载 Candidate Background 的原始或投递文档，是候选人经历与能力的来源之一；它不负责表达期望职位、薪资等求职约束。
_Avoid_: Job Search Profile、求职画像、用户账户

**Opportunity Stage（岗位旅程阶段）**:
描述 Opportunity Journey 在真实求职业务中的粗粒度进展，依次为 TARGETED、APPLIED、INTERVIEWING 和 CLOSED；它不表示面经检索等内部任务是否正在运行。
_Avoid_: 任务状态、Agent 状态、抓取状态

**Journey Workstream（旅程工作流）**:
在同一 Opportunity Journey 内可独立推进的工作流，包括面试准备、内推、投递和面试；多个工作流可以同时处于进行中。
_Avoid_: 旅程阶段、单一状态机

**Tailored Resume（岗位定制简历）**:
从不可变的基础简历和已确认 Candidate Background 中，针对一个具体 Job/JD 选择最相关的经历、
项目和技能后形成的投递版本；它可以压缩或重排事实，但不得新增未经候选人确认的经历或能力。
_Avoid_: 基础简历、虚构经历、静默覆盖原简历

**Interview Availability（面试可用时间）**:
候选人在指定日期范围内可用于面试的时间段，由日历忙闲信息、时区、工作时间和个人约束推导；
读取可用时间不等于授权向 HR 发送消息或创建、移动日历事件。
_Avoid_: 完整日历内容、已确认面试、自动接受邀请

**Task Run（任务运行）**:
某个 Agent 或业务 Tool 针对一个 Opportunity Journey 执行一次明确任务的持久化记录，包含输入
产物、输出产物、预算、检查点、尝试次数和终态；聊天中的“我完成了”不能替代 Task Run 成功。
_Avoid_: 对话轮次、Journey Stage、临时函数调用

**Journey Artifact（旅程产物）**:
由 Task Run 创建、带版本、内容哈希、来源引用和创建者记录的不可原地覆盖产物；只有通过对应
交付契约校验的版本才能被后续任务消费。
_Avoid_: 聊天摘要、可变共享字典、无来源文件

**Agent Handoff（智能体交接）**:
生产者向消费者提交一组 Journey Artifact 及交付清单，并经过结构、来源、语义、政策和消费者
输入契约校验的过程；交接成功表示产物可消费，不表示整个 Journey 已完成。
_Avoid_: transfer 消息、复制上下文、子智能体自然语言总结

**Revision Request（修订请求）**:
交接校验失败后返回生产者的结构化问题清单，指出失败规则、受影响产物和允许的修订范围；需要
新增候选人事实或高影响授权时必须转交用户，不能由 Agent 自行补全。
_Avoid_: 模糊重试、消费者静默修复、无限 Agent 循环

## 技术约束

### 禁止使用 `curl_cffi` 或类似 TLS 指纹模拟库进行数据采集

网站的反爬策略对 TLS 指纹模拟非常敏感，curl_cffi 的指纹与真实浏览器存在细微差异，极易被
识别并封锁。所有平台数据采集应优先使用 Playwright 真实浏览器（导航真实页面 + 被动旁听 API
响应），或通过 CDP 连接用户已登录的 Chrome 实例。这一约束适用于 Boss、小红书及未来可能接入
的任何平台。

### Skill 系统设计准则：纯文档、跨 Agent 可移植

Skill 只包含 `SKILL.md`（纯文档），不使用自定义代码（无 `tool.py` / `get_tools()`）。

**设计目标**：skill 可以在不同 Agent 之间移植——我们的 skill 可以直接用在 Pi、Claude Code 等
其他 Agent 上，反之亦然。

**实现方式**：
- SKILL.md 中的步骤只用通用工具（`execute`、`read_file`、`ls`、`write_file`），
  不依赖任何 Agent 特有的定制工具或代码加载机制
- Agent 通过 `read_file` 读 SKILL.md 获取说明，通过 `execute` 执行 Shell 命令完成操作
- 不需要 `load_skill_tools()`、`importlib`、`get_tools()` 等代码层面的集成

**验证**：ChromeCDP-setup skill 的工作流程（检查端口→查找 Chrome→启动→轮询）全部使用
PowerShell 命令描述，在 Pi / Claude Code（有 bash 工具）和我们的 JobAgent（有 execute 工具）
上均可直接执行。
