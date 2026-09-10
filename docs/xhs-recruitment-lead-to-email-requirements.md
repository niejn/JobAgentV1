# 小红书招人帖到 HR 邮件投递需求设计

> 状态：Ready for implementation（CLI only）
> 版本：v1.1（2026-09-08）
> 样本：[样本 A](https://www.xiaohongshu.com/discovery/item/6a82b8df0000000025014840) 、
> [样本 B](https://www.xiaohongshu.com/explore/6a2610dd000000001702d446)

## 1. 产品目标

JobAgent 能从小红书公开招人帖中识别真实招聘线索，提取正文、正文图片和评论中的岗位与
投递信息，生成可追溯的岗位 JD 和联系人资料，并基于已确认的候选人背景制作岗位定制简历
和 HR 邮件草稿。

用户历史提供或已保存的招人帖不是一次性输入：每条 `Recruitment Note` 都应抽取可复用的
岗位、技术栈、地点、经验、公司/团队特征和招聘表达特征，进入后续 XHS 招人帖查询的特征库。
用户可从已确认 Job Search Profile、直接提供的 XHS 链接或历史贴触发搜索；Agent 依据这些
XHS 范围内的已保存特征生成有界查询组合、去重后寻找相似招聘帖，不要求用户每次都重新提供链接。

本需求只服务小红书渠道，不读取 Boss JD URL 来生成查询，也不实现网页前端审批、网页 Agent
恢复、评论/私信索要邮箱或索要内推。

### 1.1 CLI 用户路径

```text
已确认 Job Search Profile / 已保存 XHS 历史贴 / 用户提供 XHS 链接
  -> XhsRecruitingAgent 生成或执行有界 XHS 搜索
  -> Recruitment Note 不可变快照
  -> 招聘判定、JD/邮箱证据、候选岗位列表
  -> 每条帖子最多选择一个最匹配岗位
  -> 创建或关联一个 Opportunity Journey
  -> 定制简历与邮件草稿
  -> CLI HITL 确认
  -> SMTP 投递与 Delivery Receipt
```

多岗位帖的岗位、职责和要求保存在 `Recruitment Note` 的候选岗位列表中，不能自动创建
Journey。每个 `Recruitment Note` 最多选择一个岗位创建或关联 Opportunity Journey；未选择岗位
只是该帖的提取结果，不产生独立对象或投递流程。

### 1.2 不在本期范围

- Boss 职位 URL/JD 到小红书的跨渠道相似搜索；
- 网页端展示、审批或恢复 CLI HITL；
- 自动评论、私信、关注作者，或向作者索要邮箱/内推；
- 依据公司名称、官网域名猜测邮箱；
- 自动发送邮件、群发邮件或 SMTP 成功后宣称 HR 已读。

“索要邮箱与内推”另见 `docs/future-work.md`。

## 2. 发现、查询改写与历史特征库

### 2.1 三种发现入口

| 入口 | 输入 | 查询策略 |
|---|---|---|
| Profile 发现 | 已确认 Job Search Profile | 从期望岗位、技术方向、城市、经验和公司偏好生成查询组合 |
| 历史贴扩展 | 已保存 Recruitment Note 特征 | 使用历史招聘语言、岗位别名、技术栈和地点寻找相似招人帖 |
| 直接读取 | 用户提供完整 XHS 链接 | 只读取该帖；可由用户后续明确要求“找类似帖子” |

每轮搜索最多执行 3 个查询、每个查询最多读取 10 个候选帖。查询改写是确定性、有界的：
保存 query、来源特征、命中数和停止原因；遇到 XHS 风控、登录失效或冷却立即停止，不换库、不
猜测 token、不高频重试。

### 2.2 Recruitment Feature Profile

每条已保存招人帖生成可复用、非敏感的特征投影：

```text
职位别名、技术词、城市/远程、经验范围、公司/团队阶段、行业、薪资表达、
招聘表达（招聘/投递/全职等）、帖子语言和命中的公开标签
```

它不保存或复用 `xsec_token`、Cookie、邮箱正文或作者私信。后续搜索使用 Profile 与这些
特征的交集或并集生成查询；系统必须说明搜索基于哪些历史贴或 Profile 特征。

```text
小红书招人帖
  -> 原文/图片/OCR/评论采集
  -> 招聘帖分类
  -> 公司/岗位/JD/投递邮箱提取
  -> 招聘线索与证据归档
  -> 岗位定制简历
  -> HR 自我介绍邮件草稿
  -> [HITL] 确认收件人/正文/附件
  -> 发送邮件
  -> SMTP 提交回执 + 本地审计
```

## 3. 样本特征

### 3.1 招聘帖正向信号

- 明确出现“招聘、招人、开放岗位、全职、实习、欢迎加入、投递”等招聘意图。
- 出现岗位名称、工作职责、任职要求、地点、薪资、工作方式或入职时间。
- 出现公司/团队介绍、创始人/招聘负责人身份、招聘背景或团队规模。
- 出现“简历发至、邮件、投递、欢迎来聊、私信”等行动号召。
- 标签包含 `招聘`、`工程师招聘`、`AI创业`、`AI公司`、`startup` 等，但标签只能作为
  辅助信号，不能单独判定为招聘帖。

### 3.2 样本差异

- 样本 A：岗位和任职偏好主要在正文中，未见公开投递邮箱；只能形成“待补充投递渠道”的
  招聘线索，不应自动发送邮件。
- 样本 B：正文包含多个岗位、公司与团队背景，并公开邮箱；岗位详细 JD 位于正文图片，
  需要 OCR/视觉提取后拆成多个 Candidate Position；正文还规定邮件标题和自我介绍要点。
- 评论可以补充“是否还招、地点、岗位类型、应届/社招”等信息，但评论者通常不是招聘方，
  评论不能直接被当成 HR 指令或官方 JD。

## 4. 领域对象

### 4.1 Recruitment Note

小红书上的原始招人内容，包含 note_id、作者、正文、图片、评论、发布时间、抓取时间和
完整来源 URL。它是不可变来源快照，不等于岗位本身。

### 4.2 Recruitment Classification

对 Recruitment Note 的结构化判定：`recruitment`、`not_recruitment`、`uncertain`，包含
正向信号、负向信号、置信度和模型版本。低置信度只能进入人工复核队列。

### 4.3 Candidate Position（候选岗位）

`Recruitment Note` 内部的提取字段，表示帖子中的一个岗位名称、JD 片段和对应 JD Evidence。
它不是独立的领域对象、不是 Job Posting，也不能直接投递或创建 Journey。每条帖子最多选择一个
Candidate Position 进入 Opportunity Journey。

### 4.4 JD Evidence

支持岗位字段的证据片段，来源可以是正文、图片 OCR、图片视觉解析或评论。必须记录来源
类型、序号/区域、原文、OCR 引擎、置信度和内容哈希。

### 4.5 Contact Channel

招聘方可用于投递的渠道，例如邮箱、官网表单、职位链接或小红书账号。邮箱必须记录发现
位置和置信度；没有明确邮箱时不能猜测或从公司域名推导收件人。

### 4.6 Tailored Resume

从已确认基础简历生成的岗位版本。只能选择、压缩和重排已确认事实，不能新增技能、项目、
公司、学历或业绩。

### 4.7 Application Email Draft

针对一个选定 Candidate Position 和一个 Contact Channel 生成的邮件草稿，包含收件人、主题、正文、附件、
简历版本、事实来源、来源帖子和创建时间。草稿不等于已发送。

### 4.8 Email Delivery

一次实际 SMTP 投递尝试，记录草稿版本、附件哈希、SMTP 提交结果、Message-ID、错误和最终
本地状态。SMTP 成功只表示邮件被邮件服务器接受，不表示 HR 已阅读或回复。

## 5. 用户故事

- 用户给出一条小红书招人帖，Agent 能判断它是不是招聘内容并说明依据。
- 用户希望一条帖子中的多个岗位形成候选岗位列表，而不是把全部 JD 混成一个岗位。
- 用户希望 Agent 能读懂正文图片里的 JD，并保留每个字段来自哪张图、哪一块区域。
- 用户希望从正文和评论中找到投递邮箱；找不到时，系统明确告诉用户“没有公开邮箱”。
- 用户希望 Agent 根据 JD 调整简历，但不能虚构经历或能力。
- 用户希望 HR 邮件有针对性地介绍自己，主题、正文和附件都能在发送前完整预览。
- 用户希望发送后能查询投递记录，但系统不把 SMTP 成功说成 HR 已读。

## 6. 端到端工作流

### 6.1 获取和归档

1. 只接受用户提供的完整小红书 URL；保留 `xsec_token`，不使用裸 note_id 访问详情。
2. 保存正文、作者、发布时间、来源 URL、抓取时间、图片原文件、图片哈希和评论快照。
3. 页面/图片/评论采集失败时保留部分结果，并标记缺失项；不伪造完整内容。
4. 对同一 note_id 做幂等，重复抓取只新增版本或新评论，不覆盖原始快照。
5. 评论使用已登录 Chrome/CDP 的真实页面或页面内接口采集：默认读取前 20 条可见顶级评论，
   页面仍有更多时继续滚动/分页，最多 50 条。保存稳定评论 ID（若有）、作者、父评论关系、
   文本和抓取顺序。只有作者 ID 与帖主一致的评论/回复进入 JD、邮箱和招聘证据包；游客评论
   只计入抓取覆盖统计，不进入模型提取或投递决策。
6. 保存 `comment_capture_status`（`complete`、`partial`、`blocked`、`not_available`）、读取数量、
   是否仍有更多、停止原因和恢复游标（若有）。评论不完整时只能说“已检查 N 条评论”，不能说
   “帖子没有邮箱”。

### 6.2 招聘帖识别

1. 对正文、图片 OCR 文本和评论分别提取招聘信号。
2. 使用确定性规则 + LLM 结构化判定，输出 `classification`、置信度和证据。
3. 用户求职自述、招聘服务广告、培训广告、泛行业讨论和二手转发默认降级为 `uncertain`。
4. `not_recruitment` 和低于阈值的 `uncertain` 不进入简历/邮件流程。

### 6.3 JD 提取与单 Journey 选择

1. 正文先进行段落级提取。
2. 图片按原始顺序下载并 OCR；记录图片哈希、OCR 引擎、语言、版本和置信度。
3. OCR 置信度低、表格/复杂排版或字段冲突时，使用视觉模型复核或转人工。
4. 将“公司背景”“岗位列表”“统一福利”“单岗位职责”“任职要求”“投递要求”分开。
5. 多岗位帖子拆成 Recruitment Note 内的 Candidate Position 列表；统一福利只能继承到明确标注适用的岗位。
6. 每个 Candidate Position 生成结构化 JD 和 Markdown 报告，并引用 JD Evidence。
7. 按 Job Search Profile 对同帖 Candidate Position 排序。只有最匹配且用户明确选择的一个岗位可采用；
   若用户未选择，状态为 `needs_review`，不得创建 Journey 或发送邮件。

### 6.4 投递邮箱提取

按可信度排序：

1. 正文明确写出的邮箱：高可信度。
2. 图片 OCR/视觉识别出的邮箱：中高可信度，必须保留图片证据。
3. 帖主/招聘方明确回复中的邮箱：中可信度，必须记录作者身份和评论 ID。
4. 游客评论中的邮箱：不作为本期证据，不展示为可投递渠道。
5. 从公司名称、官网域名或猜测规则推导出的邮箱：禁止使用。

模型提取前，采集层把正文、全部已下载图片 OCR 和已抓到的评论编号为证据片段。邮箱提取结果
必须经过格式校验、去重、域名规范化和来源一致性检查。无高/中高可信度邮箱且评论抓取完整时，
流程停在 `contact_missing`；评论未抓完整时为 `contact_unknown`，两者都不得自动发邮件。
用户可明确要求继续抓取下一页评论，直到预算、风控或页面终止。

第一阶段只搜索三种公开来源：正文、正文图片/OCR/视觉提取、评论。不得向作者评论、私信或
打招呼索取邮箱，也不得索要内推；这些动作属于独立 Future Work，不是 `contact_missing` 的
自动补救。

### 6.5 定制简历和邮件

1. 第一阶段用户将已有 PDF 简历放入受控简历目录；Agent 只能列出目录内可用 PDF 的文件名、
   大小和 SHA-256，不得接受任意路径或读取目录外文件。
2. Agent 向用户展示候选列表；用户明确选择一个文件名后才可创建邮件草稿。第一阶段不改写、
   不生成 PDF，也不从 JD 推断候选人事实。
3. Tailored Resume 的事实映射、内容改写和 Kami 排版由后续 ResumeCraftingAgent 完成。
4. 生成邮件主题：优先遵循帖子指定格式，否则使用“应聘岗位 + 姓名”。
5. 邮件正文至少包含：目标岗位、与岗位最相关的 2–3 个事实、可验证的成果/作品、开始时间
   或地点信息（仅在已知时填写）、附件说明和联系方式。
6. 邮件正文应针对该岗位和公司，禁止空泛模板、夸大背景和虚构项目。
7. 邮件和附件进入 `draft`，用户确认后才可发送。
8. 草稿生成成功时，CLI 必须立即完整展示收件人、来源帖子、公司、岗位、主题、全文正文、
   附件文件名、大小和 SHA-256；不得以普通进度摘要替代草稿审阅。

### 6.6 CLI HITL、邮件发送和跟进

1. CLI 先展示可用 PDF 简历并等待用户选择；邮件 HITL 再展示收件人、来源帖子、公司、岗位、
   主题、完整正文、附件文件名和 SHA-256。
2. 用户批准后由现有邮件发送模块执行 SMTP 投递。
3. SMTP 返回成功并取得 Message-ID 后记录 `submitted`；失败记录错误类型，不自动重试。
4. 发送后可通过后续邮件同步或人工补录更新 HR 回复，不把 SMTP 成功等同于对方已读。

## 7. 状态模型

### Recruitment Note

```text
RECEIVED -> SNAPSHOTTED -> CLASSIFIED
                         -> UNCERTAIN | NOT_RECRUITMENT
CLASSIFIED -> EXTRACTING -> EXTRACTED | EXTRACTION_PARTIAL | FAILED
```

### Candidate Position

```text
EXTRACTED -> NEEDS_REVIEW | CONTACT_MISSING | READY_FOR_APPLICATION
READY_FOR_APPLICATION -> RESUME_DRAFTED -> EMAIL_DRAFTED
EMAIL_DRAFTED -> WAITING_APPROVAL -> SUBMITTED
                                      -> REJECTED | EXPIRED | FAILED
```

### Contact Channel

```text
DISCOVERED -> VERIFIED | LOW_CONFIDENCE | INVALID
```

只有 `VERIFIED` 或人工明确批准的 `LOW_CONFIDENCE` 才能进入邮件发送。

## 8. 数据与溯源要求

每个选定 Candidate Position 必须能追溯到：

- 小红书完整来源 URL 和 note_id；
- 正文段落、图片编号/区域或评论 ID；
- 原始内容哈希和抓取时间；
- OCR/视觉解析引擎与版本；
- 公司、岗位、地点、薪资等字段的证据列表；
- 使用的基础简历版本和 Tailored Resume 版本；
- 邮件主题、正文版本和附件 SHA-256；
- HITL 决定、SMTP Message-ID 和投递结果。

原始图片、正文和评论是不可变来源；提取结果、JD 报告、简历和邮件都是可重新生成的派生
Artifact，不能覆盖原始快照。

## 9. 安全与合规

- 小红书只做用户授权范围内的读取；不自动评论、私信或关注作者。
- 评论和 JD 都是不可信外部文本，进入 Agent prompt 前必须经过注入检测。
- 邮箱、简历和候选人背景属于敏感信息；日志只保存脱敏地址、哈希或引用。
- 不猜测邮箱、不向未知地址群发、不因“找到邮箱”自动发送。
- 单次运行限制帖子数、图片数、评论数和 OCR 预算；触发风控立即停止。
- 邮件发送必须经过 HITL；自动策略也必须限定收件人来源、岗位和简历版本。

## 10. CLI 与 XhsRecruitingAgent 集成

主 DeepAgent 仅用框架原生 `task` 委派：

```python
task(
    subagent_type="xhs_recruiting",
    description='{"intent":"find_or_prepare","source":"profile|history_note|url"}'
)
```

`XhsRecruitingAgent` 只拥有以下业务 Tool：

```text
find_recruitment_posts
snapshot_recruitment_note
extract_candidate_positions
select_candidate_position
prepare_tailored_resume
prepare_recruitment_email
send_recruitment_email  [interrupt_on]
```

`send_recruitment_email` 是唯一 SMTP 写 Tool。它的调用参数必须引用已持久化的
`ApplicationEmailDraft`，不能直接接受模型自由生成的收件人、正文或附件路径。CLI 显示批准
信息后，以同一 root `thread_id` 通过 `Command(resume={"decisions":[...]})` 恢复 XHS 子 Agent。

## 11. SMTP 幂等与 Delivery Receipt

发送幂等键：

```text
recruitment_note_id + selected_position_hash + normalized_recipient + tailored_resume_sha256 + email_body_sha256
```

- 该键已有 `submitted` 或 `confirmed` Receipt 时，阻止再次发送；
- SMTP 明确接受且有 Message-ID：`submitted`，不等同 HR 已读；
- SMTP 超时、连接中断或 Message-ID 缺失：`unverified`，不自动重试；
- SMTP 明确拒绝或配置失败：`failed`，只能重新 prepare 后再次 CLI 批准；
- 用户拒绝：`rejected`，保留草稿与拒绝时间，不发送。

### 11.1 邮件发送职责边界

`EmailSender` 是内部 SMTP Adapter，不是 Agent Tool。早期通用 `send_application_email` 不再暴露给
主 Agent、Boss 子 Agent 或 XHS 子 Agent，因为它允许模型直接给出任意收件人、正文和附件路径，
并在 Tool 内猜测登记册身份。

XHS 唯一邮件发送入口为：

```text
send_recruitment_email(draft_id)
  -> XhsEmailApplicationService.send(draft_id)
  -> EmailSender
  -> DeliveryReceipt
  -> ApplicationStateProjector
```

`XhsEmailApplicationService` 是确定性应用层：读取持久化草稿，校验 XHS `note_id`、选定岗位、
verified 邮箱、受控 PDF 和幂等键；它不让模型重新提供收件人、正文或附件路径。

`ApplicationStateProjector` 是需要新开发的确定性状态投影模块。它消费 DeliveryReceipt，以
`xhs:<note_id>` 为唯一 Job 身份，更新 `job_records`、关联 Opportunity Journey 和投递状态事件；
它不使用 LLM，也不由模型在发送后自行选择下一步 Tool。

```text
submitted  -> job_records / Journey = applied
failed     -> 仅记录失败回执，Journey 不标 applied
unverified -> 仅记录不确定回执，不自动重试、不标 applied
rejected   -> 仅记录拒绝，未发生外发
```

## 12. 验收标准

### 帖子识别

- 样本 A 被识别为招聘帖，至少提取 Agent 工程师和增长运营两个 Candidate Position；默认不创建
  Journey，直到用户选择其一。
- 样本 B 被识别为招聘帖，正文邮箱被提取为高可信度 Contact Channel。
- 样本 B 图片中的多个岗位 JD 能按图片证据拆分为 Candidate Position。
- 普通评论不会直接升级为官方 JD 或高可信度邮箱。

### 简历与邮件

- 没有公开邮箱时，状态为 `contact_missing`，不创建可发送邮件。
- 定制简历只能引用 Candidate Background 已确认事实。
- 邮件主题遵循帖子约定；正文能指出岗位相关匹配点和事实来源。
- 用户批准前不会发送邮件或修改外部平台数据。
- 同一 Recruitment Note + 选定 Candidate Position + 简历版本 + 收件人不会重复发送。

### 可靠性

- 重复抓取同一帖子不会重复创建 Candidate Position 列表或 Journey。
- OCR 失败、评论不完整或邮箱解析失败时，结果明确标记部分失败。
- SMTP 超时或结果不确定时状态为 `unverified`，不自动重试。
- 发送结果可关联来源帖子、岗位、简历版本和 Message-ID。

## 13. 实施切片

1. Recruitment Note snapshot：正文、图片、评论、可复用查询特征和幂等归档。
2. 招聘帖分类器：规则信号、置信度和人工复核。
3. 图片 OCR/视觉提取：图片证据和字段 provenance。
4. 多岗位 Candidate Position 列表与 JD 结构化。
5. 邮箱提取、验证和 Contact Channel 置信度。
6. Tailored Resume 生成和事实映射。
7. HR Email Draft 生成与 HITL 预览。
8. SMTP 投递、审计、幂等和失败恢复。
9. Agent Tool 接入：`find_recruitment_posts`（可从 Profile/历史贴特征生成查询）、`extract_candidate_positions`、
   `prepare_recruitment_email`、`send_recruitment_email`。

## 14. 自动化回归与完成定义

自动化测试使用 Fake XHS Reader、Fake OCR 和 Fake SMTP，覆盖：

- Profile/历史贴特征生成有界查询，不跨渠道读取 Boss JD；
- 同帖多个 Candidate Position 时最多一个岗位被选择并创建 Journey；
- 无可信邮箱停在 `contact_missing`；普通评论邮箱不能自动发送；
- 用户批准前 SMTP Fake 不执行；同 thread 的 `Command(resume)` 后仅发送一次；
- 同一幂等键阻止重复发送；SMTP 不确定结果不自动重试。

完成定义：

给定一条包含正文、图片和评论的公开小红书招人帖，JobAgent 能准确识别招聘意图，生成可追溯
的一个或多个 Candidate Position，提取可信投递邮箱，基于候选人已确认事实生成岗位定制简历和有针对性
的 HR 邮件，并在用户明确批准后发送；任何证据不足、字段冲突、邮箱不可信或发送结果不确定
的情况都不会自动猜测、群发或伪报成功。
