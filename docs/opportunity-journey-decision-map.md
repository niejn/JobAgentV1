# Opportunity Journey 决策图

本图记录 JobAgent 从“自动投递工具”演进为岗位求职旅程 Agent 时必须逐项解决的产品决策。每次只推进最前沿的一张票据。当前实施前前沿为 #7（真实性研究）与 #18（OCR 原型），建议按 #7 → #18 推进。

## #1: 核心工作流边界是什么？

Blocked by: —
Type: Grilling

### Question

系统应围绕一个无边界的综合 Agent，还是围绕一个具体岗位的完整求职生命周期组织能力？

### Answer

采用 `Opportunity Journey（岗位求职旅程）`：每个具体岗位拥有独立、持久化的旅程，从岗位发现、面试调研与准备，延伸到内推、投递、面试、结果追踪和复盘。能力可以分阶段交付，但都挂接在同一个旅程上。

## #2: 第一阶段交付到什么结果？

Blocked by: #1
Type: Grilling

### Question

第一阶段的“准备模拟面试”是只生成可复习、可用于未来模拟的准备包，还是必须包含实时交互式模拟面试？

### Answer

第一阶段生成 `Interview Preparation Pack（面试准备包）`，包含真实面经证据、常见问题、岗位能力矩阵、候选人薄弱点、学习计划和未来模拟面试可用的脚本；不包含实时语音或视频模拟面试。后续模拟面试 Agent 直接消费该准备包。

## #3: 哪些资料可以进入真实面经证据集？

Blocked by: #2
Type: Grilling

### Question

什么公司/岗位匹配范围可以被称为目标岗位的真实面经；完全找不到真实面经时系统如何降级？

### Answer

A 级为同公司、同岗位；B 级为同公司、相近岗位或同职能。仅 A/B 可以进入真实面经证据集，其他公司的同类岗位绝对禁止。每条证据保留来源 URL、发布时间、抓取时间和匹配理由。没有 A/B 时生成 Evidence Gap Report，并只依据目标公司官方 JD、技术栈和招聘资料生成明确标注“基于 JD 推断”的 JD Preparation Pack。

## #4: 第二阶段如何接入职位发现与内推？

Blocked by: #2, #3, #14
Type: Grilling

### Question

职位发现、员工发现、内推联系和简历发送分别在哪些状态进入 Opportunity Journey，哪些动作必须经过 HITL？

### Answer

待决策。

## #5: 面试记录与复盘的授权边界是什么？

Blocked by: #2
Type: Research

### Question

录音、录屏、题目提取、个人弱项分析与长期学习记录应采用什么授权、隐私和数据保留规则？

### Answer

待研究。

## #6: 第一阶段如何创建岗位求职旅程？

Blocked by: #2, #8
Type: Grilling

### Question

职位自动发现尚未实现时，用户应提供哪些最小信息来创建 Opportunity Journey 并启动面经检索？

### Answer

用户提供目标公司、具体岗位名称、官方 JD URL 或完整 JD 文本（二选一）；职级、地点和岗位编号可选。旅程引用全局 Job Search Profile 和用户确认后的 Candidate Background。当前 `Profile` 模型混合了这两类信息，实施时需要拆分。

## #7: 如何验证面经来源真实性？

Blocked by: #3, #6, #9
Type: Research

### Question

匿名帖子、转载、营销内容和过期内容分别如何评分，哪些来源可以进入 A/B 证据集，是否需要交叉验证？

### Answer

待研究。

## #8: 求职画像、候选人背景与简历如何分工？

Blocked by: #6
Type: Grilling

### Question

如何拆分当前混合的 Profile，并从简历建立可供面试准备使用的候选人背景？

### Answer

拆成全局 Job Search Profile 与 Candidate Background。第一阶段支持 PDF/DOCX Resume，并以纯文本作为 fallback；解析结果先形成背景草稿，必须由用户确认后才能成为 Candidate Background，任何 Agent 不得静默改写候选人的经历或技能。岗位定制简历留待后续阶段。

## #9: 第一阶段采用什么面经来源架构？

Blocked by: #3, #6
Type: Grilling

### Question

第一阶段应把面经检索写死为小红书，还是先定义统一来源协议并只交付小红书实现？

### Answer

定义统一 `InterviewExperienceSource` 协议，第一阶段只交付小红书实现。后续增加牛客、脉脉等来源时不重写 Opportunity Journey 流程；所有来源遵守 A/B-only 证据规则。

## #10: 生成准备包前是否强制人工审核面经？

Blocked by: #3, #7, #9
Type: Grilling

### Question

系统筛选出 A/B 面经后，是自动生成带引用的准备包，还是必须让用户逐条确认面经后才能继续？

### Answer

自动生成，不要求逐条确认。每个问题和结论必须链接到具体证据，展示 A/B 等级、匹配理由和置信度；低置信度内容不得进入“真实面经”部分。用户可排除可疑帖子并重新生成。

## #11: 第一阶段采用什么交互与输出形式？

Blocked by: #2, #6, #10
Type: Grilling

### Question

第一阶段只提供 CLI 与文件产物，还是同时建设 Web UI？

### Answer

第一阶段 CLI-only。输出 Markdown 供用户阅读，输出 JSON 保存证据、匹配等级和结构化准备结果，供后续 Agent 使用；等旅程状态模型稳定后再建设可视化看板。

## #12: Opportunity Journey 如何持久化？

Blocked by: #1, #11
Type: Grilling

### Question

旅程状态、证据索引和准备包元数据如何存储，大文件如何关联，并如何保留未来数据库升级能力？

### Answer

第一阶段使用 SQLite 保存状态与索引；简历原文件、Markdown、来源快照以及未来录音/录屏保存在文件系统，数据库记录路径、摘要和元数据。领域逻辑不直接依赖 SQLite，后续可增加 MySQL 或 PostgreSQL 实现。

## #13: Redis 在未来部署中扮演什么角色？

Blocked by: #12
Type: Grilling

### Question

Redis 是否只用于未来的缓存、任务队列、限流和分布式锁？

### Answer

是。Redis 不作为 Opportunity Journey 的持久化事实源，第一阶段不安装、不连接也不实现 Redis，只保留未来扩展边界。

## #14: Opportunity Journey 如何表达业务进度？

Blocked by: #1, #2, #4
Type: Grilling

### Question

面试准备、寻找内推和正式投递可能同时进行，旅程应采用单一线性状态，还是顶层阶段加独立工作流状态？

### Answer

采用“顶层业务阶段 + 并行 Journey Workstream”。顶层阶段为 TARGETED → APPLIED → INTERVIEWING → CLOSED；面试准备、内推、投递和面试分别维护独立状态。第一阶段只实现 TARGETED 和面试准备工作流。

## #15: 第一阶段 MVP 的完成边界是什么？

Blocked by: #2, #3, #6, #8, #9, #10, #11, #12, #14, #16
Type: Grilling

### Question

第一阶段是否以手工创建目标岗位旅程、确认候选人背景、检索小红书 A/B 面经、保留原始来源、生成可追溯准备包或官方 JD 降级包并持久化为完整边界？

### Answer

确认。第一阶段不包含职位自动发现、找员工、联系内推、自动发简历、实时模拟面试、录音录像、Rate Limiter 和 Redis。按增量步骤交付：Step 1 建立 Opportunity Journey、全局画像/候选人背景和手工 JD 输入的最小骨架；Step 2 完成小红书帖子正文与图片下载、图片 OCR/多模态识别、Raw Source Snapshot 与识别结果落库；Step 3 再完成 A/B 证据筛选以及 Markdown/JSON Interview Preparation Pack 生成。来源适配器必须先保存 Raw Source Snapshot（包括原始图片），再生成标准化 Interview Evidence 和 Derived Preparation Artifact。

## #16: 第一阶段原始面经保存到什么粒度？

Blocked by: #9, #12
Type: Grilling

### Question

Raw Source Snapshot 是否只保存检索和生成实际需要的结构化文本、评论与元数据，还是同时下载完整 HTML、图片和视频等页面资源？

### Answer

保存来源平台、帖子 ID、URL、检索关键词、标题、完整正文、标签、作者公开元数据、发布时间、抓取时间、原始结构化响应、内容哈希和适配器版本。必须按帖子内顺序下载并保存原始图片，记录来源 URL、MIME、尺寸和哈希；第一版对图片执行本地 OCR 或多模态识别。视频不下载，只保留视频元数据或来源链接；第一阶段不抓取评论、私信或聊天记录。

## #17: 后期检索基础设施如何演进？

Blocked by: #12, #15, #16
Type: Research

### Question

何时需要从 SQLite/文件检索升级到向量数据库（如 Milvus）、图数据库或 GraphRAG，各方案分别解决什么规模与查询问题？

### Answer

第一阶段不引入这些基础设施；保留稳定 ID、内容哈希和 provenance，使原始快照与标准化证据未来可重新索引。具体升级门槛待研究。

## #18: 第一版图片内容提取采用什么引擎？

Blocked by: #16
Type: Prototype

### Question

面对小红书中文长图、聊天截图和复杂排版，应以本地 OCR 为主、多模态 LLM 为主，还是采用置信度驱动的混合策略？

### Answer

第一版必须完成图片内容识别。采用本地 OCR 优先；只有本地结果置信度不足或遇到复杂排版时，才在用户明确启用后调用外部多模态 LLM。所有结果记录引擎、模型或版本、置信度和原图引用。具体本地 OCR 引擎仍需用真实小红书图片样本做原型比较。

## #19: 第一阶段保存哪些作者与互动数据？

Blocked by: #16, #18
Type: Grilling

### Question

为支持证据追溯，第一阶段保存哪些作者标识和帖子互动内容，同时避免提前收集内推阶段才需要的社交数据？

### Answer

保存来源平台、作者用户名或显示名、作者 ID、帖子 URL，以及作者与帖子之间的关系。下载面经帖子正文和承载面试内容/面试题的帖子图片；不下载作者头像，不抓取评论、私信或聊天记录。聊天数据只属于后续找人内推阶段。

## #20: 跨信息源请求频率如何治理？

Blocked by: #9, #13
Type: Grilling

### Question

小红书、Boss、公司官网/ATS 和未来来源的请求频率控制，是由各 Backend 自行实现，还是提供统一的 Rate Limiter；第一阶段是否交付？

### Answer

后期提供统一、可注入的 `SourceRateLimiter`，按来源、账号和操作类型控制频率、并发、随机抖动、退避、冷却与熔断，避免限流逻辑散落在具体 Backend 和 Opportunity Journey 业务流程中。第一阶段不实现 Rate Limiter，也不引入 Redis；后期先采用单进程本地实现，只有多进程或多实例部署时才增加 Redis 协调实现。
