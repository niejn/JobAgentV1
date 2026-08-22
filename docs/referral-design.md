# Opportunity Journey 小红书面经与内推通道设计

> 状态：分阶段实施中。第一阶段优先完成 JD 驱动的面经发现、原始内容留存、
> 图片 OCR/多模态识别和面试准备包；职位发现与内推在后续阶段接入同一个
> Opportunity Journey。

## 1. 动机与核心思路

Boss 直聘海投转化率低；小红书上有大量员工发布的内推帖（`#内推` `#社招`），
内推简历通常直达部门、有真实 HC。但内推帖有个天然缺陷：

**帖子不包含具体岗位信息**（实测样例：*"研发 测试 产品经理 设计 销售等各岗位都有"*），
联系方式也不在正文--求职者在评论区问「社招吗」「微信扫？」，实际触达走**私信**。

因此核心流程是：

```
小红书内推帖 ──LLM提取──> ReferralOffer(公司/部门/岗位方向/是否有效)
                                │
                                ▼
                    岗位交叉匹配（解决"没有岗位详情"问题）
                    ├── 来源A：Boss直聘已抓取岗位（按公司名归一化匹配）★ v1
                    ├── 来源B：公司官网 ATS（Moka/北森/Greenhouse）   v2
                    └── 来源C：帖内附带的投递链接/邮箱                v1 顺带提取
                                │
                                ▼
                LLMMatcher 对候选岗位打分（复用现有引擎）
                                │
                                ▼
                触达（分级策略，见 4.4）+ HITL 人工确认检查点
```

**用户价值**：联系内推人时带上「我想投 XX 岗位（附 Boss/官网链接）」比泛泛的
「求内推」回复率显著更高--这正是「先找到岗位再联系内推」的意义。

### 1.1 第一阶段：由具体 JD 反向发现面经

固定拼接 `公司 + 岗位 + 城市 + 面经` 只能作为采集链路验证和 LLM 不可用时的
fallback，不能作为正式的相关性策略。它只能保证结果较新，并不能保证帖子是该岗位的
真实面试经验。例如「公司招聘讨论」可能包含所有关键词，却没有任何面试过程或题目。

正式流程以 Opportunity Journey 中的具体 Job/JD 为输入：

```text
Job/JD
  -> JD 事实解析（公司、业务线、岗位、级别、地点、技术栈）
  -> Interview Evidence Research Loop
       -> Reason：分析当前证据缺口和上一轮失败/低质原因
       -> Act：生成本轮 InterviewSearchPlan 并调用 Spider_XHS
       -> Observe：去重、下载正文/图片、OCR、相关性与质量判定
       -> Update：更新已准入 A/B 证据、覆盖度和搜索历史
       -> 达标则停止；否则调整关键词或过滤条件进入下一轮
  -> Markdown + JSON Interview Preparation Pack
```

这里有一个刻意的顺序：小红书面经经常只存在于图片中，因此不能只根据标题和正文决定
是否下载。通过元数据预筛的候选帖需要先下载到原始快照区，再做 OCR/多模态识别和深度
重排。被判为不相关或营销内容的原始快照仍保留，并记录拒绝理由，避免重复抓取；但它们
不能进入 Interview Evidence 或准备包。

### 1.2 自适应 Interview Evidence Research Loop

这不是“先让 LLM 一次性想完所有关键词，再按列表抓取”的流水线，而是一个有界的 ReAct
循环。LLM 每轮都必须看到目标 JD、已执行查询、候选帖拒绝原因、已准入证据摘要和
`EvidenceCoverage`，然后只规划下一轮最有价值的动作。例如：

- 搜到大量“卖资料”帖子：增加排除词、提高作者风险阈值或换用“面试轮次/业务线”查询。
- 公司命中但岗位不准：从 JD 提取岗位别名、部门、技术栈并收紧岗位条件。
- 同一篇帖子被多个词反复召回：利用 `note_id` 和内容哈希去重，转向未覆盖主题。
- 已有足够后端面经但缺少系统设计题：下一轮只补“系统设计/架构/场景题”等缺口。
- 精确岗位资料稀缺：保持同公司约束，按 A 级到 B 级逐步放宽岗位相似度，不允许降级到
  其他公司的 C 级资料。

循环由专门的研究模块管理，外层 JobAgent 只调用一次
`discover_interview_evidence(company, role, city, job_description)`。搜索、下载、OCR、评估和
迭代策略属于该深模块的内部实现，不能变成要求用户手工调用的一组命令。

每轮生成的 `InterviewSearchPlan` 是下一组动作，而不是完整研究结果。LLM 输出必须结构化，
示例：

```json
{
  "job_id": "boss-123",
  "iteration": 2,
  "coverage_gaps": ["系统设计", "二面/终面"],
  "lessons_from_previous_round": ["精确岗位结果少", "卖资料帖子占比高"],
  "company": "字节跳动",
  "company_aliases": ["字节", "ByteDance", "抖音"],
  "role": "后端开发工程师",
  "role_aliases": ["后端开发", "服务端开发", "Java后端", "Golang后端"],
  "business_terms": ["抖音电商", "商业化"],
  "technology_terms": ["Go", "Redis", "Kafka", "分布式"],
  "seniority_terms": ["实习", "校招"],
  "location_terms": ["北京"],
  "queries": [
    {"kind": "interview_stage", "text": "字节 服务端 二面 终面"},
    {"kind": "coverage_gap", "text": "抖音电商 后端 系统设计 面试"}
  ],
  "exclude_terms": ["付费", "资料包", "培训"],
  "round_budget": {"queries": 2, "details": 10}
}
```

生成约束：

- 公司、地点、业务线、技术栈等事实优先来自 JD；LLM 扩展的别名必须与原事实分开记录。
- 生成多组短查询，覆盖精确岗位、岗位别名、业务线、技术栈、面试轮次和职级，不生成一条
  过长且必须同时命中所有词的查询。
- 城市不是每条查询的强制词。面经可能不写城市，把城市强塞进全部查询会降低召回率。
- 每个计划设置查询数、每查询结果数和总详情抓取数预算；第一阶段按顺序执行，不并发轰炸。
- 相同 `note_id` 跨查询去重，并记录它由哪些 query 召回，作为可解释的相关性信号。
- LLM 不可用、结构化输出失败或超预算时，退回确定性模板查询；fallback 结果仍必须经过后续
  OCR 和相关性判定。

停止不能只依赖 LLM 输出一句“够了”，必须同时受结构化条件和硬预算约束：

1. **成功停止**：达到配置的独立 A/B 证据数量，并且目标维度覆盖率达到阈值；相同作者、
   同一内容的转载或高度重复帖子不能重复计数。
2. **边际收益停止**：连续若干轮没有新增合格证据或覆盖度没有提升，生成 Evidence Gap Report。
3. **预算停止**：达到最大轮数、搜索数、详情抓取数、OCR 图片数、模型 token/费用或总耗时。
4. **来源风险停止**：遇到验证码、`SECURITY_BLOCK`、Cookie 失效或来源冷却要求时立即暂停，
   不允许 LLM 自行绕过风控。
5. **人工停止**：用户取消、修改目标岗位或要求先查看中间结果。

因此“高质量且数量够”被定义为可审计的 `EvidenceCoverage` 与准入证据集合，而不是模型不可
复现的主观感觉。LLM负责提出下一步和解释判断，程序负责去重、预算、安全边界和停止规则。

### 1.3 帖子相关性重排与证据准入

筛选分两层，避免仅凭关键词直接下载并进入准备包：

1. **轻量预筛**：发布时间、note_id 去重、标题/正文基础关键词、视频排除、明显卖资料信号。
2. **深度判定**：输入 JD、正文、标签、作者最小信息、图片 OCR/多模态结果，由 LLM 输出
   结构化的 `PostRelevanceAssessment`。

建议评分维度：

| 维度 | 建议权重 | 说明 |
|---|---:|---|
| 公司匹配 | 30% | 公司、品牌或业务线是否指向目标公司 |
| 岗位匹配 | 25% | 同岗位、岗位别名或相近职能 |
| 技术栈匹配 | 20% | 与 JD 核心技术和职责的重合度 |
| 面试证据真实性 | 15% | 是否包含轮次、问题、过程、结果等真实经历信号 |
| 新鲜度 | 10% | 越接近当前招聘周期越优先 |
| 商业/资料风险 | 扣分或拒绝 | 营销 CTA、付费资料、培训学员案例、跨公司批量运营 |

证据准入规则沿用领域词汇：

- A 级：同公司、同岗位，可进入 Interview Preparation Pack。
- B 级：同公司、相近岗位或同职能，可进入，但必须显示差异。
- 其他公司同类岗位、通用八股、招聘讨论、卖资料内容：保留原始快照和拒绝理由，不作为证据。
- 没有 A/B 级内容时生成 Evidence Gap Report；可以另行生成 JD Preparation Pack，但必须明确
  标注“基于 JD 推断”，不能伪装成真实面经。

`PostRelevanceAssessment` 至少包含：`company_score`、`role_score`、
`technology_score`、`experience_signal_score`、`freshness_score`、`seller_risk`、
`evidence_grade`、`decision`、`reasons` 和引用到正文/OCR 片段的证据位置。

### 1.4 当前实现与目标设计的关系

当前内部采集能力已实现 Spider_XHS 最新排序、公司/岗位/城市/关键词检索、用户 ID 帖子
检索、新鲜度筛选、账号级卖资料启发式过滤以及正文/图片下载。正式入口是 JobAgent 的
`discover_interview_evidence` Tool；`xhs-search`/`xhs-download` 仅保留为隐藏诊断命令。
当前固定关键词组合只解决“采集链路能否运行”，属于 fallback。

下一步不是继续堆固定关键词，也不是只增加一个一次性 Planner，而是增加：

1. 持久化的 `InterviewResearchState`（轮次、查询历史、拒绝原因、证据集合、覆盖度、预算）；
2. 根据反馈生成下一轮 `InterviewSearchPlan` 的 ReAct 决策器；
3. 多查询执行、跨查询去重和召回来源记录；
4. 图片 OCR/多模态内容提取并与 Raw Source Snapshot 关联；
5. `PostRelevanceAssessment`、A/B 证据准入和 `EvidenceCoverage` 计算；
6. 确定性停止策略以及 Markdown + JSON Interview Preparation Pack 生成。

## 2. 实测数据（2026-08 采样）

通过 agent-reach/OpenCLI 搜索「内推」验证：

| 帖子 | 公司信息 | 联系方式 |
|---|---|---|
| "(长期有效，皆可投递)zilliz公司岗位内推" | 公司明确，岗位只有大类 | 无（评论区问，走私信/二维码） |
| "Kimi code 招人，速来" | 公司可推断(Moonshot) | 无正文联系方式 |
| "阿里千问招Agent实习生｜可内推" | 公司+岗位方向明确 | 无 |
| "Minimax基础架构系统组招人啦！" | 公司+部门明确 | 无 |

结论：
- 公司名提取可行（标题即含公司名或品牌名，需别名归一化：`阿里千问`->`阿里巴巴`）
- 岗位粒度最多到「部门/方向」，具体岗位必须交叉匹配
- 触达员工的稳定通道是私信/评论区，且大量内推帖的模式是**「评论区留言，发帖人私信你」**

风控实测（重要）：
- 连续读取笔记详情/评论两次触发 `SECURITY_BLOCK`--**只读操作就会被限流**，
  写操作（评论/私信）风控阈值只会更低
- 私信机审实时扫描内容：微信号/手机号/二维码(OCR)/外链，谐音变体（V我、伽薇）
  均已收录；批量相似文案直接判营销号；另有举报触发的人工复核
- 商业动机：私信导流 = 平台零收益，因此私信是全平台风控最强区域；
  对比 Boss「打招呼」是平台原生业务功能--同一动作，XHS 视为营销行为

## 3. 新增域模型（`domain.py`）

```python
class ReferralPost(BaseModel):
    id: str                      # XHS note id（确定性 id，用于防重复）
    author_name: str
    author_id: str               # XHS user id，私信定位用
    title: str
    content: str
    tags: list[str]
    likes: int
    published_at: datetime | None
    top_comments: list[str]      # 评论区前 N 条（辅助提取 & 活跃度判断）

class AuthorProfile(BaseModel):
    author_id: str
    nickname: str
    description: str = ""        # 主页简介（营销词检测）
    ip_location: str | None      # IP 属地（S6 信号）
    recent_note_titles: list[str] # 最近帖子标题（S1/S2 多公司与规律检测）

class ReferralOffer(BaseModel):
    post_id: str
    company: str                 # 归一化后公司名
    company_raw: str             # 帖子原文中的公司表述
    departments: list[str]       # 部门/方向，如 "基础架构"
    role_types: list[str]        # 社招/实习/校招
    is_active: bool              # LLM 判断：是否"长期有效"、近期是否还招
    posted_at: datetime | None
    confidence: float            # 提取置信度
    scam_risk: float             # 帖子级骗局风险分（4.5.1）
    authenticity: str            # employee / recruiter / business / unknown（4.5.2）
    authenticity_score: float
    red_flags: list[str]         # 判定证据，供 CP-1 人工复核
    companies_promoted: list[str]  # 该作者推过的所有公司（S1 信号输出）
    apply_link: HttpUrl | None   # 帖内若附投递链接/邮箱
    stale_days: int              # 发帖至今天数（超阈值降权，默认 >90 天跳过）

class ReferralRequest(BaseModel):
    offer: ReferralOffer
    job: Job | None              # 交叉匹配到的具体岗位（可 None）
    match_score: float | None
    message_draft: str           # 起草的评论/私信文案
    status: Literal["draft", "confirmed", "sent", "skipped", "replied"]
```

## 4. 模块布局

```
jobagent/
  scraper/
    xhs.py                 # XhsScraper: 搜索 + 笔记正文 + 评论抓取（Playwright + cookie）
  referral/
    __init__.py
    extractor.py           # LLM: ReferralPost+AuthorProfile -> ReferralOffer（公司归一化 + 4.5 反诈）
    job_crossref.py        # 公司名归一化 + Boss 岗位池匹配
    messenger.py           # 评论/私信草稿生成（LLM）+ 分级触达发送
    history.py             # 复用 ApplyHistory 模式，路径 ~/.jobagent/referral_history.json
```

### 4.1 XhsScraper

- 复用 `browser_login.PLATFORM_CONFIG` 增加 `"xhs"` 平台
  （login_url: `https://www.xiaohongshu.com`，success_selectors: `.side-bar-user`，
  key_cookies: `web_session`），`jobagent login --platform xhs` 即可交互登录
- 搜索入口 `https://www.xiaohongshu.com/search_result?keyword={quote(关键词)}`
- 除笔记正文/评论外，还需抓取发帖人主页（`fetch_author_profile`）：
  昵称、简介、IP 属地、最近 10 篇帖子标题 -> 供 4.5.2 账号真实性识别
- 搜索关键词策略：`["{company} 内推", "{desired_role} 内推", "内推 社招"]`
- 反爬：XHS web 对无头/高频极敏感（实测连续读操作即被 block）。**必须**：
  - headed 模式或 `--disable-blink-features=AutomationControlled` + 真实 UA
  - 每次抓取间隔随机分钟级，单次 run 读操作上限（默认 30 帖 + 每帖评论 20 条）
  - 出现验证码/风控页 -> 复用 `captcha.py` 通知 + 暂停
- 备选通道：OpenCLI / xiaohongshu-mcp（若用户已配置，可作 backend 抽象，同 LLM 三通道模式）

### 4.2 LLM 提取（extractor.py）

Prompt 输入：笔记标题 + 正文 + tags + 前 20 条评论。
输出 JSON（ReferralOffer 字段 + `company_aliases: list[str]` + `scam_risk`）。
复用 `ClaudeClient`/`streaming.py`；JSON 解析须剥离 markdown fence。

过滤规则（发帖人优先级）：
1. 帖子明确是「我司内推」（排除面经/吐槽/中介），`scam_risk` 低于阈值
2. `is_active` 且 `stale_days <= 90`
3. 作者主页若可判断为员工认证/企业号 -> 加分

### 4.3 岗位交叉匹配（job_crossref.py）

公司名归一化三层策略：
1. 内置别名表（`字节|字节跳动|bytedance` -> `字节跳动`，覆盖头部 50 家）
2. LLM 判断（别名表未命中时，一次调用判多个候选对）
3. 失败则该 offer 标记 `job=None`，触达降级为「求推荐方向内岗位」

匹配源 v1 = 复用 `BossScraper` 抓该公司岗位（`query=公司名`），产出
`(offer, job, match_score)` 三元组；已有当日抓取缓存的岗位池直接复用。

### 4.4 触达分级策略（Human-in-the-Loop，v1 核心需求）

> 需求来源：XHS 私信是平台风控最强区域（见 §2 风控实测），全自动私信封号风险高。
> 缓解思路：**降低触达频率 + 让对方主动联系我 + 所有关键动作人工确认**。

触达方式分三级，由配置 `REFERRAL_CONTACT_MODE` 选择：

| 级别 | 方式 | 风险 | 说明 |
|---|---|---|---|
| **L0 评论钩子** ★默认 | 在内推帖下自动评论简短求内推（如「求内推，3 年后端 Python」） | 低 | 实测大量内推帖作者的模式就是「评论区扣 1 我私你」-> **对方私信我**，私信发起方是对方，完全绕开我方批量私信风控；评论属正常互动行为 |
| **L1 半自动私信** | LLM 起草私信 -> Telegram 推送草稿（含帖子链接+作者主页+文案）-> 人工确认/复制粘贴发送 | 低 | 人工发送自带真实节奏；岗位链接等敏感内容只出现在人工私信里 |
| **L2 全自动私信** | Playwright 自动私信 | 高 | 默认关闭；需 `REFERRAL_CONTACT_MODE=auto_dm` + `XHS_AUTO_DM=true` 双开关显式开启；每日 ≤10 条、分钟级随机间隔、LLM 逐条差异化文案、避开联系方式/二维码词汇、建议非主力号 |

**L0 评论的约束（写操作风控）：**
- 每日 ≤5 条，间隔分钟级随机
- LLM 生成差异化文案（禁止模板复读，防「相似评论折叠」）
- 评论内**禁止出现任何链接/联系方式**（评论带外链是导流特征，比私信更敏感）
- 具体岗位信息不进评论；等对方私信后，再在**人工回复**中发送岗位链接

**L0 的已知副作用与对策（实测）：** 在内推帖下评论后，除发帖人外还会有大量
骗局营销号主动私信（「简历辅导/包内推」收费 4-5k 套餐）。对策见 4.5 反诈过滤。

**HITL 人工确认检查点（贯穿全流程）：**

| 检查点 | 时机 | 人工动作 |
|---|---|---|
| CP-1 offer 审核 | LLM 提取 ReferralOffer 后 | Telegram 收到 offer 摘要，可否决错判的公司 |
| CP-2 草稿审核 | 生成评论/私信草稿后 | Telegram 收到草稿，编辑或确认发送 |
| CP-3 回复接管 | 对方私信回复后 | 系统仅提醒（含对方主页链接+疑似骗局标记），人工接管对话并发送岗位详情 |

CP-3 中的岗位资料包（岗位名+链接+匹配理由+简历要点）由系统预生成，
人工复制即用--保证「先找到岗位再联系内推」的核心价值落到人工对话里。

草稿模板变量：`$company` `$role` `$job_title` `$job_link` `$name` `$skills_top3`，
默认文案示例（L1/L2 私信基线，LLM 在此基础上逐条差异化）：

> 您好！看到您发布 $company 内推，我对 $job_title（$job_link）很感兴趣。
> 我是 $name，$years 年经验，主要技术栈 $skills_top3。方便的话想请您帮忙内推，感谢！

### 4.5 反诈过滤（post 级 + 账号级）

#### 4.5.1 帖子级骗局特征（scam_risk）

实测发现大量内推骗局帖（《小红书内推套路》419 赞帖曝光）。LLM 提取时须同时
输出 `scam_risk` 分数，命中以下特征则跳过或标记：

- 话术：「不需要经验」「留子友好」「永久 remote」「有 mentor 带」「保证进」
- 帖子 IP 与公司所在地不符（如「纽约投行」帖 IP 在国内）
- 评论区出现收费服务导流（改简历/辅导面试/付费套餐）

#### 4.5.2 账号真实性识别（authenticity）★ 核心需求

> 目标：只要**真实在该公司工作的员工**，排除做内推生意的运营号。

核心判据（用户规则）：**真员工只推自己公司**。A 公司员工不可能长期发
B、C、D 公司的内推；一个人推多家公司 = 内推生意/中介/猎头，不是员工内推。

数据来源：XhsScraper 抓取发帖人主页（`fetch_author_profile`），
获取：昵称、简介、IP 属地、最近 N 篇帖子标题。

LLM 综合以下信号输出 `authenticity`（`employee` / `recruiter` / `business` /
`unknown`）+ `authenticity_score` + `red_flags`（证据列表，供 CP-1 人工复核）：

| # | 信号 | 判定 | 权重 |
|---|---|---|---|
| S1 | **多公司内推**：作者近期帖子推 ≥2 家不同公司 | `business`（内推生意） | 最高（一票否决员工身份） |
| S2 | **发帖规律**：高频（周多发）、规律间隔、批量同模板（「XX公司内推」系列） | `business` | 高 |
| S3 | **用户名/简介特征**：含「求职辅导/简历修改/职业规划/内推/实习保offer」等营销词 | `business` | 高 |
| S4 | **公司一致性**：发帖内容集中在单一公司+单一业务领域（如全是千问相关） | `employee` 加分 | 高 |
| S5 | **职业痕迹**：主页有日常工作/技术分享/生活内容，非纯招聘号 | `employee` 加分 | 中 |
| S6 | **IP 属地**：作者 IP 与公司办公地一致 | `employee` 加分 | 中 |
| S7 | **账号身份**：企业号/认证营销号 | `recruiter`/`business` | 中 |
| S8 | **互动模式**：作者在评论区让加微信/引导私信收费 | `business` | 高 |

决策规则：
- S1 命中 -> 直接 `business`，不进入触达流程
- `authenticity != employee` 或 `authenticity_score < 0.6` -> 跳过或降级为 CP-1 人工裁定
- 灰区（unknown）：不自动触达，仅出现在 `referral search` 报告中供人工判断

CP-3 收到陌生私信时同样提示用户警惕收费内推类对话。

### 4.6 配置新增

| 变量 | 默认 | 说明 |
|---|---|---|
| `XHS_COOKIE` | - | web_session cookie（同 boss_cookie 模式） |
| `XHS_REFERRAL_MAX_POSTS` | 30 | 单次抓帖上限 |
| `XHS_REFERRAL_STALE_DAYS` | 90 | 帖子过期阈值 |
| `REFERRAL_CONTACT_MODE` | `comment` | 触达方式：`comment`(L0) / `dm_review`(L1) / `auto_dm`(L2) |
| `XHS_COMMENT_DAILY_LIMIT` | 5 | L0 每日评论上限 |
| `XHS_AUTO_DM` | false | L2 二次确认开关（高危，双开关保护） |
| `REFERRAL_MESSAGE_TEMPLATE` | 内置 | 私信模板（L1/L2 草稿基线） |
| `REFERRAL_MIN_MATCH_SCORE` | 0.7 | 交叉匹配岗位的投递门槛 |
| `REFERRAL_SCAM_SKIP` | true | 跳过高骗局风险帖 |

### 4.7 CLI

```bash
jobagent login --platform xhs                      # 交互登录保存 cookie
jobagent referral search --query "内推 大模型"      # 只抓帖+提取，打印 offer 列表
jobagent referral run --profile me.yaml \          # 完整链路：交叉匹配 + 草稿 + HITL 通知
    --query "内推" --companies "字节跳动,阿里"
```

## 5. 风险与合规

| 风险 | 等级 | 缓解 |
|---|---|---|
| XHS 搜索/评论/私信自动化违反平台条款，封号 | 高 | 触达分级（默认 L0 评论钩子）+ HITL 检查点 + 各级限流；自动私信双开关；提示用户用小号 |
| 评论触发骗局营销号私信骚扰 | 高（实测） | 4.5 反诈过滤 + CP-3 提醒用户警惕收费内推 |
| 把骗局帖当成真内推（收费陷阱/信息窃取） | 中 | scam_risk 评分 + 特征词库 + 默认跳过 |
| 内推帖信息过期（HR/员工早已离职） | 中 | stale_days 过滤 + 礼貌措辞（"若已失效请忽略"） |
| 公司名匹配错误->联系时投错岗位 | 中 | 置信度阈值 + CP-1 人工审核 |
| 抓取触发 XHS 风控（实测连续读操作即被 block） | 高 | 复用 captcha 检测+暂停通知；单次 run 读操作上限 + 分钟级间隔 |
| 评论公开暴露求职状态（现同事可见） | 低 | 文档明示该副作用；提供 `REFERRAL_CONTACT_MODE=dm_review` 替代 |
| 员工隐私（仅使用其公开发布的内推意愿信息） | 低 | 不存储个人信息超出帖子本身 |

## 6. 实施切分

- **PR-1**：域模型（含 AuthorProfile/authenticity 字段）+ XhsScraper（搜索/正文/评论/作者主页）+ `login --platform xhs`
- **PR-2**：extractor.py（LLM 提取 + 帖子级反诈 + 账号真实性识别 S1-S8）+ `referral search` 命令
- **PR-3**：job_crossref.py（别名表 + Boss 交叉匹配）+ 草稿生成 + Telegram HITL 通知（CP-1/CP-2）
- **PR-4**：L0 评论钩子（低频自动评论 + 历史去重）
- **PR-5**（可选，高危）：L2 自动私信
- **PR-6**（可选）：官网 ATS 抓取、收件箱监听辅助 CP-3
