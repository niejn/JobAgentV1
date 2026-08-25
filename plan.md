# JobAgent 开发计划 / Plan

> 活文档：随进展更新。✅ 完成 / 🚧 进行中 / ⏸ 待决策 / 📋 待办

---

## 1. 当前状态快照（2026-08-21）

- 代码基线：main @ `887b57c`，约 2400 行核心代码 + 986 行测试
- ✅ 第一阶段单 Agent 面经研究与准备链路完成（见 §8.1）
- ✅ Spider_XHS 真实搜索/详情/下载、Tesseract OCR、火山 GLM-5.3 和全链路冒烟通过
- ✅ 全仓验证：127 passed / 1 skipped；Ruff、mypy、diff check 全绿
- 存量投递链路 P0 仍按 §3 独立追踪，不属于第一阶段的只读面经研究范围
- Referral（小红书内推通道）设计定稿：`docs/referral-design.md`
- **需求总结定稿：`docs/referral-requirements.md`**（用户故事 FR-1~8 / NFR / 验收标准）
- PR-1 已写了一半（见 §4），**按决定暂停，先完成 §2 环境设置再继续**

---

## 2. 环境设置（当前阻塞项）⏸

开发环境 `.venv` 已建（uv + Python 3.11），但依赖不完整/不兼容：

| # | 事项 | 状态 | 说明 |
|---|---|---|---|
| S-1 | langchain 版本二选一 | ⏸ 待决策 | 代码用 0.3.x API（`langchain.schema`/`init_chat_model`），venv 里装成了 1.3.15（`langchain.schema` 已移除）。**A.** pyproject 锁 `>=0.3.20,<0.4`（最小改动）／**B.** 升级代码适配 1.x |
| S-2 | `playwright install chromium` | ✅ 已完成 | Chromium 1234、headless shell、FFmpeg 安装于 `~/.jobagent/browsers`；真实 Boss Cookie 登录态验证通过 |
| S-3 | XHS 账号准备 | ⏸ 待用户 | 建议**非主力小号**（风控设计前提）；备好后 `jobagent login --platform xhs` 扫码 |
| S-4 | LLM 通道确认 | ✅ 已决策 | 第一版仅 OpenAI-compatible；Claude/Anthropic 后期再接 |
| S-5 | `.env.example` 补 XHS 变量 | 📋 待办 | `XHS_COOKIE`、`XHS_REFERRAL_MAX_POSTS`、`XHS_REFERRAL_STALE_DAYS`、`XHS_SCRAPE_DELAY_MIN/MAX` |
| S-6 | 依赖约束修正 | 📋 待办 | 随 S-1 决策一起改 pyproject.toml |
| S-7 | Spider_XHS library 接入 | 📋 待办 | 已在 `D:/mashibing/xiaohongshu_crawler/Spider_XHS`；作为 JobAgent 进程内 Python library 直接 import，将 PyExecJS、curl_cffi 等依赖合并进项目环境；验证登录、搜索和详情各一条跑通 |

**验收标准**：`pytest` 全绿 + `jobagent --help`、`jobagent login --help` 可跑 + `chromium` 就位。

---

## 3. 存量 P0/P1 缺陷（代码审查产出，见对话记录）📋

| 级别 | 问题 | 位置 |
|---|---|---|
| P0 | CLI 无条件传 `gpt-4o-mini` 给 LLMMatcher，Claude 后端必然调用失败 | `cli.py` `_run_pipeline` |
| P0 | 每投一个岗位开关一次浏览器 | `cli.py` 循环内 `async with` |
| P0 | 防重复投递跨运行失效（Job.id 用 uuid4 + 历史路径是相对路径） | `domain.py` / `applier/history.py` |
| P1 | 搜索 URL 未编码、Boss 城市代码硬编码深圳 | `scraper/boss.py` 等 |
| P1 | top_matches 截断与投递判断不一致（第 6 名高分被静默丢弃） | `cli.py` |
| P1 | LLM 返回 JSON 未剥 markdown fence，解析失败静默 score=0 | `matcher/llm_matcher.py` |
| P1 | today_count 时区错位（UTC 存储 vs 本地日期比较） | `applier/history.py` |
| P1 | `$name` 模板变量语义与 README 不符（HR 名 vs 候选人名） | `applier/boss.py` |

⏸ 决策项：先修 P0 再开发新功能，还是与 referral PR 合并处理？

### 3.1 代码整改追踪表（2026-08-20 review）📋

> 状态约定：`[ ]` 待办 / `[-]` 进行中 / `[x]` 完成。关闭条目时须同时补测试，并在“验证”列记录命令或用例。

| ID | 优先级 | 状态 | 需要修改的代码 | 目标与验收标准 | 验证 |
|---|---|---|---|---|---|
| CR-01 | P0 | [ ] | `applier/linkedin.py`、`cli.py`、`README.md` | LinkedIn Easy Apply 当前只是返回 `DRAFT` 的占位实现。二选一：实现真实提交和成功确认；或从 CLI 投递流程禁用并将 README 能力标记降为“规划中”。不得再把 `DRAFT` 计为已投递。 | 新增 LinkedIn 成功、不可申请、外链申请和失败路径测试 |
| CR-02 | P0 | [ ] | `applier/boss.py`、`tests/test_boss_applier.py` | 点击发送后必须通过“已发送消息/会话状态/服务端成功响应”之一确认成功；找不到输入框不得直接写 `submitted`。只有正向确认后才能写 ApplyHistory。 | 增加发送失败、选择器漂移、无输入框、网络错误测试 |
| CR-03 | P0 | [ ] | `domain.py`、`applier/history.py`、各 scraper | Job 使用稳定业务 ID（平台 + 平台岗位 ID，或规范化 URL hash），历史文件改到固定用户数据目录；跨进程重启后仍能去重。 | 相同岗位跨两次运行生成同一 ID；临时 cwd 改变不影响历史路径 |
| CR-04 | P1 | [ ] | `auth/browser_login.py`、`tests/test_browser_login.py` | XHS Cookie 校验必须要求可见的登录态元素；登录/登出信号都缺失时返回“不确定/无效”，不能因占位 redirect pattern 返回 True；风险控制页应判失败。 | 补 hidden 登录元素、未知 DOM、登录弹窗、风控页测试 |
| CR-05 | P1 | [ ] | `matcher/llm_matcher.py`、`cli.py`、相关测试 | 不再由 CLI 无条件覆盖 Claude 模型；按实际 backend 选择模型。LLM 输出使用结构化 schema，至少兼容 markdown fence；岗位描述作为不可信数据分隔，降低 prompt injection 影响。 | 覆盖 OAuth/Anthropic/OpenAI 三后端、fenced JSON、恶意岗位描述、非法字段类型 |
| CR-06 | P1 | [ ] | `applier/history.py`、`tests/test_apply_history.py` | `applied_at` 与 `today_count` 使用同一明确时区；保存采用临时文件 + 原子替换，并为多进程访问增加锁，避免重复投递和 JSON 损坏。 | 覆盖 UTC/Asia-Shanghai 午夜边界及两个并发 writer |
| CR-07 | P1 | [ ] | `scraper/boss.py`、`scraper/linkedin.py`、测试 | 使用 `urllib.parse.urlencode` 等标准方式构造查询；Boss 城市代码由 location 映射或配置提供，不再硬编码深圳。 | 覆盖中文、空格、`&`、`#` 及多个城市 |
| CR-08 | P1 | [ ] | `cli.py`、匹配/流水线测试 | 明确“展示 Top 5”和“达到阈值即可投递”是两个概念；不得因 `top_matches[:5]` 让第 6 个高分岗位静默变成 0 分。 | 构造 6 个均高于阈值的岗位，验证预期投递数量 |
| CR-09 | P1 | [ ] | `cli.py`、scraper/applier 生命周期 | 每个平台一次运行只启动一次 Playwright/browser context 管理器，不在每个岗位循环中反复启动与关闭浏览器。 | 多岗位测试断言 browser launch/close 各一次 |
| CR-10 | P2 | [ ] | `config.py`、`applier/boss.py`、`README.md` | 统一问候模板语法和 `$name` 语义：配置说明与 README 使用同一套 `$company/$title/$candidate_name/$hr_name` 变量；未知变量给出明确错误。 | 模板变量、缺失变量、转义 `$` 测试 |
| CR-11 | P2 | [ ] | `scraper/xhs.py`、`domain.py`、`tests/test_xhs_scraper.py` | `comments_count` 表示平台真实评论数，另设 captured/top comments 数量；抓取作者 nickname；`published_at` 统一为 timezone-aware datetime。 | 新增评论总数、昵称和时区序列化测试 |
| CR-12 | P2 | [ ] | `pyproject.toml`、开发环境说明 | 确保 `uv sync --dev` 后可直接运行 pytest、Ruff、mypy；当前 `.venv` 缺 Ruff/mypy。解决 §2 的 LangChain 版本决策后锁定依赖。 | `pytest -q`、`ruff check .`、`mypy jobagent` 全部通过 |

**建议实施顺序**：CR-01 → CR-02 → CR-03 → CR-04 → CR-05/06 → CR-07/08/09 → CR-10/11 → CR-12。

**当前验证基线（2026-08-20）**：`pytest -q` 为 **70 passed, 1 skipped**；跳过项是 Windows/NTFS 不执行 POSIX 文件权限断言。未发现真实硬编码 API Key；命中 token 均为测试假数据。

**关闭标准（Definition of Done）**：代码、测试、README/配置说明同步更新；`pytest`、Ruff、mypy 全绿；不得降低现有风控、每日限额或人工确认保护。

---

## 4. Referral（小红书内推通道）路线图

设计文档：`docs/referral-design.md`（含风控实测数据、触达分级 L0/L1/L2、HITL 检查点 CP-1/2/3、帖子级+账号级反诈）

### 4.0 XHS 爬虫方案调研结论（2026-08-20）✅

调研了 `D:/mashibing/xiaohongshu_crawler` 下 4 个开源项目，**决定不自研签名，采用 Backend 适配器复用 Spider_XHS**：

| 项目 | 结论 |
|---|---|
| **Spider_XHS**（cv-cat, 7.3k★，8-18 仍在更新） | ★技术首选：纯 HTTP + curl_cffi TLS 指纹 + 本地 JS 签名（PyExecJS/需 Node）；API 与需求一一对应：`search_some_note`/`get_note_info`/`get_note_all_out_comment`/`get_user_all_notes`；只需完整登录 cookie；返回结构化 JSON 无需解析 DOM，可直接作为 Python library 接入 |
| MediaCrawler | 功能完整但架构较重，不作为首选 |
| XHS-Downloader | 偏重已知链接的媒体下载，不适合作为搜索与详情主 Backend |
| xhs_one_spider | 核心实现未公开，无法作为 JobAgent library 接入 |

**架构决策**：`XhsBackend` 协议 + 双实现
- `SpiderXhsBackend`（默认）：在 JobAgent 进程内直接 import Spider_XHS，调用 `XHSPcAuth` / `XHS_Apis`；同步网络调用通过 `asyncio.to_thread()` 接入现有异步流程
- `PlaywrightXhsBackend`（回退）：PR-1 的 DOM 方案，无外部依赖
- Spider_XHS 与 JobAgent 共用 Python 环境、日志、Cookie 和进程状态；不建设 subprocess/stdin/stdout bridge，也不增加隔离层
- 依赖要求：将 Node.js（PyExecJS 签名）、`curl_cffi` 等 Spider_XHS 依赖纳入 JobAgent 的环境配置和版本验证
- **最大化复用约束**：`SpiderXhsBackend` 必须是薄适配器。鉴权、签名、HTTP 会话、搜索/详情/分页、已有重试、帖子字段标准化以及满足要求的媒体下载优先直接调用 Spider_XHS；禁止在 JobAgent 中复制或重写同等逻辑。JobAgent 只新增 Opportunity Journey 编排、域模型映射、Raw Source Snapshot/内容哈希与 provenance、OCR/多模态识别及 SQLite/文件存储。若 Spider_XHS 现有函数缺少必要能力，优先通过小范围扩展或贡献上游补齐，再在适配器中做最小差异处理。

### 4.1 跨信息源请求治理 🚧

- ✅ 第一阶段已为 `SpiderXhsBackend` 接入 `pyrate-limiter` 4.4 内存桶：初始化、搜索、详情、作者帖子走 API 桶；图片逐张调用 Spider_XHS `download_media` 并在每张图前取得媒体桶许可。
- ✅ API 与媒体桶频率分别通过 `.env` 配置；没有引入 Redis，也没有在研究业务流程里散落固定 `sleep`。
- 后期扩展为统一 `SourceRateLimiter`，覆盖小红书、Boss、公司官网/ATS 以及未来新增的信息源；按 `source + account + operation` 分别控制频率和并发。
- 能力应包含：最小请求间隔、令牌桶或滑动窗口、并发上限、随机抖动、429/风控响应退避、冷却窗口和熔断；读、搜索、详情和未来写操作使用不同策略。
- 限流器位于来源适配器/请求传输边界，不进入 Opportunity Journey 领域模型；Backend 在发起外部请求前调用 `acquire()`，调度结果和风控事件可被观测与审计。
- 当前只实现单进程本地限流，不建设分布式协调；需要多进程或多实例部署时再增加 Redis/PostgreSQL 实现。

### PR-1 域模型 + XhsScraper + login 🚧（已暂停，待 §2 设置）

已完成：
- ✅ 需求文档更新：账号真实性识别 S1-S8 信号（多公司内推一票否决等），见设计文档 §4.5.2
- ✅ `domain.py`：`ReferralPost` / `AuthorProfile` / `ReferralOffer`（含 `scam_risk`/`authenticity`/`companies_promoted`）/ `ReferralRequest` + 两个枚举
- ✅ `models/__init__.py` re-export 新模型
- ✅ `auth/browser_login.py`：XHS 平台配置（登录成功检测走选择器）+ `cookies_valid` 支持选择器式校验（登录弹窗不跳转 URL 的 SPA 场景）
- ✅ `auth/cookie_manager.py`：xhs 平台接入（web_session cookie）
- ✅ `config.py`：`xhs_cookie`、抓帖上限、过期天数、分钟级抓取间隔配置
- ✅ `scraper/xhs.py`：`XhsScraper`（搜索/笔记正文/评论/作者主页，风控标记检测 `XhsRiskControlError`，分钟级 pacing）
- ✅ `cli.py`：`login --platform xhs`
- ✅ `tests/test_xhs_scraper.py`（11 用例）；`test_browser_login.py` 权限用例 Windows 跳过
- ✅ 单测全绿：70 passed, 1 skipped

未完成（待 §2 设置完成后继续）：
- ⏸ ~~XHS 搜索/详情页真实选择器校准~~ -> 方案已变更：改为实现 `XhsBackend` 协议 + `SpiderXhsBackend`（见 §4.0），Playwright 版降级为回退实现
- ⏸ `XhsBackend` 协议定义 + 现有 `XhsScraper` 重构为 `PlaywrightXhsBackend`
- ⏸ 端到端冒烟：`jobagent login --platform xhs` -> 抓 1 帖走通（两条 backend 各验一次）

#### SpiderXhsBackend 实施记录（2026-08-20）✅

- ✅ 新增进程内 `XhsBackend` Protocol 与 `SpiderXhsBackend` 薄适配器，直接复用 `XHSPcAuth`、`XHS_Apis`、`handle_note_info`、`download_note` 和 Spider_XHS HTTP client `close()`。
- ✅ 新增 `jobagent xhs-download <URL>`：保存 `detail.txt`、`info.json`、`raw_response.json` 和帖子全部图片。
- ✅ 真实图文帖冒烟成功：帖子 `694e0cb50000000022030726`，正文与 18 张图片落盘；图片全部非空（合计 2,070,598 bytes），两个 JSON 文件可解析，无临时文件残留。
- ✅ 验证：Ruff 通过；完整测试 `76 passed, 1 skipped`。
- 📋 Playwright 回退版重命名/协议适配仍留在后续工作，不影响 SpiderXhsBackend 默认路径。

### 后续 PR 📋

| PR | 内容 | 备注 |
|---|---|---|
| PR-2 | `referral/extractor.py`：LLM 提取 + 帖子级反诈（scam_risk）+ 账号真实性（S1-S8）+ `referral search` 命令 | JSON 解析须剥 fence |
| PR-3 | `referral/job_crossref.py`（公司别名表 + Boss 岗位交叉匹配）+ 草稿生成 + Telegram HITL（CP-1/CP-2） | 复用 LLMMatcher |
| PR-4 | L0 评论钩子：低频自动评论（每日 ≤5、差异化文案、禁链接）+ 历史去重 | 默认触达方式；注意：API 通道无评论能力时需 Playwright |
| PR-5 | L2 全自动私信（双开关、每日 ≤10、分钟级随机间隔） | 高危，最后做 |
| PR-6 | 官网 ATS 抓取；收件箱监听辅助 CP-3 | 可选 |
| Future | 跨信息源 `SourceRateLimiter`：本地限流、退避、冷却与熔断；多实例阶段再接 Redis | **不属于第一阶段** |

---

## 5. 待用户决策汇总 ⏸

1. **langchain**：A 锁 0.3 / B 升 1.x？
2. **XHS 小号**：是否已备好？
3. **LLM 通道**：Claude OAuth / Anthropic Key / OpenAI？
4. **P0 修复时机**：先修 / 合并到 referral 开发中？
5. **分支策略**：`feat/referral-xhs` 分支 or 直接 main？
6. **XHS 抓取后端**：✅ 已确认采用进程内直接 import 的 SpiderXhsBackend，Playwright 作为回退；不使用子进程 bridge

---

## 6. 风险提示（长期有效）

- XHS 风控极强：实测连续**只读**操作即触发 SECURITY_BLOCK；一切写操作（评论/私信）必须走分级触达 + HITL
- 评论钩子的已知副作用：会引来收费骗局号私信 -> 靠反诈过滤 + CP-3 提醒
- 自动化操作违反各平台 ToS：仅限个人授权范围内使用（README 合规声明）

---

## 7. OpenAI-compatible 国内模型重构（2026-08-20）🚧

### 目标

- `.env` 可显式选择 `openai-compatible`，配置模型名、API Key 和 Base URL。
- 同一套调用支持 OpenAI、DeepSeek、火山方舟等兼容 Chat Completions 的服务。
- LangChain、LangGraph、Deep Agents、langchain-openai 使用当前最新稳定版并同步锁文件。
- 第一版只保留 OpenAI-compatible 路径；Claude OAuth/Anthropic 降为后期需求，不进入当前
  Agent 配置和模型选择。
- CLI 不再无条件把 `gpt-4o-mini` 覆盖到 Claude 或其他 provider。
- 缺少对应凭证时启动即给出可执行的配置错误，日志不得输出 API Key。

### 非目标

- 本阶段不实现不同任务使用不同模型、模型路由、自动降级或成本调度。
- 不验证真实付费模型调用；默认验收全部使用离线 HTTP mock。
- 本阶段只安装并验证 Agent 生态基础依赖，不实现新的 LangGraph/Deep Agents 工作流。

### 分阶段交付与证据

| 阶段 | 交付 | 验收命令/结果 |
|---|---|---|
| LLM-1 | 配置解析与显式 provider 选择 | `pytest tests/test_llm_config.py -q` |
| LLM-2 | OpenAI-compatible Chat Completions 客户端 | `pytest tests/test_openai_compatible.py -q` |
| LLM-3 | LLMMatcher 接入统一客户端，修复 CLI 模型覆盖 | `pytest tests/test_llm_matcher.py -q` |
| LLM-4 | 最新稳定 LangChain/LangGraph/Deep Agents 依赖与 lock | `uv sync --dev` + 版本导入检查 |
| LLM-5 | `.env.example`、README 国内服务配置示例 | 文档 diff 检查，不包含真实密钥 |
| LLM-6 | 全量离线回归 | Ruff、`git diff --check`、`pytest -q` |

### 最终验收记录

- 状态：LLM provider、稳定依赖、JobAgent 对话骨架和首个业务 Tool 已实施；JD Query
  Planner/OCR 属于下一切片。
- 稳定依赖：LangChain 1.3.16、LangGraph 1.2.11、Deep Agents 0.7.8、
  langchain-openai 1.6.0；`uv sync --dev` 可复现。Deep Agents 的传递依赖可能包含其他
  provider 包，但 JobAgent 第一版不调用它们。
- 离线构造：DeepSeek 与火山方舟 Base URL 均成功构造 `ChatOpenAI`，未发起付费请求。
- 回归：`95 passed, 1 skipped`；Windows/NTFS 跳过 POSIX 权限测试。
- 质量：本次新增 Agent/Tool/LLM/Profile 范围 Ruff 与 mypy 全绿；全仓 mypy 尚有 21 条
  存量 scraper/applier/CLI 类型错误，未在本次扩大修改。
- 外部集成：DeepSeek、火山方舟和 OpenAI 的真实付费调用默认不执行。

---

## 8. JobAgent Agent-first 重构（2026-08-21）🚧

### 产品入口

用户面对的是一个围绕 Opportunity Journey 工作的 **JobAgent**，而不是一组需要记忆参数的
爬虫命令。CLI 只保留启动 Agent 会话、平台登录、配置校验和开发诊断；`xhs-search`、
`xhs-download` 等采集能力降级为内部/隐藏诊断入口，正式工作流由 Agent Tool 调用。

### 深模块与 seam

```text
User conversation
  -> JobAgent（对话、缺失信息追问、旅程编排、HITL）
     -> Opportunity Tools（稳定业务 interface）
        -> InterviewEvidenceDiscovery
           -> bounded ReAct research loop
              -> next-query planner / SpiderXhsBackend / OCR
              -> Relevance Assessment / Evidence Coverage / stop policy
        -> JobDiscovery
        -> InterviewPreparation
        -> ReferralDiscovery（后续）
```

- Tool 是业务 seam，参数使用公司、岗位、JD、Journey ID 等用户语义，不暴露下载目录、页码、
  xsec token 或 Spider_XHS 调用细节。
- LangChain Tool 是第一版 Adapter；未来 MCP Server 只是同一 Tool interface 的另一 Adapter，
  不复制业务逻辑。
- 第一版 Agent 只获得显式注册的求职工具，不启用 Deep Agents 默认文件系统/shell 工具。
- 第一阶段保持单一 JobAgent，先跑通 JD → 面经研究/OCR → 准备包；不因最终架构需要多 Agent
  就提前增加委派和上下文传递复杂度。
- LangGraph/Deep Agents 用于后续长任务、持久化检查点、子 Agent 和 HITL。升级时由主
  JobAgent 作为 Supervisor，把既有业务 Tool 分配给专业子 Agent，不重写采集、OCR、证据准入、
  简历或投递逻辑。

### 启动上下文

- `Job Search Profile`：全局求职意向（期望岗位、城市、薪资、工作制度、行业和排除条件）。
- `Resume`：用户原始简历文件。
- `Candidate Background`：从简历解析并经确认的经历、项目、技能和成果。
- 启动配置只引用上述文件及默认 Journey 存储位置；不得继续用一个 `Profile` 同时表示求职
  意向和候选人经历。

### 迁移阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| JA-1 | Python 包、项目名、命令、环境变量 `jobagent -> jobagent`；保留必要旧配置迁移读取 | 全仓无非兼容用途的旧名称；导入与 CLI smoke 通过 |
| JA-2 | 统一 OpenAI-compatible 模型构建器 | 火山/DeepSeek 离线构造测试通过 |
| JA-3 | `JobAgent` 对话运行时 + 只读 `discover_interview_evidence` Tool | Fake model/tool 集成测试可完成一次追问和调用 |
| JA-4 | 启动配置拆分 Job Search Profile、Resume、Candidate Background | 配置校验能明确报告缺失文件和字段 |
| JA-4.5 ✅ | Journey/Task Run/Artifact 状态骨架、SQLite 业务状态与 SQLite LangGraph checkpointer | 重启后按 thread_id 恢复；任务只通过 accepted Artifact 完成 |
| JA-5 ✅ | 有界 ReAct 面经研究循环、OCR、相关性准入、覆盖度停止策略和准备包工具化 | 质量反馈驱动改写检索；覆盖/边际收益/迭代预算停止；输出可追溯准备包 |
| JA-6 ✅ | 单 Agent 面试准备：题目整理、缺失答案补全、JD 重点与候选人薄弱点分析 | 每题分别记录原帖答案、模型补充答案和 source_note_ids |
| JA-7 | 岗位定制简历 Tool | 基础简历不可变；按 JD 选择约 3 个最相关项目形成草稿，禁止虚构，使用前人工确认 |
| JA-8 | 投递与旅程追踪 Tool | 每次外部投递均 HITL；记录岗位、简历版本、投递结果和后续状态 |
| JA-9 | 日历读取与面试协调 Tool | 先只读 free/busy；HR 回复和创建/修改日历事件均 HITL |
| JA-10 | Deep Agents Supervisor + 专业子 Agent 渐进拆分 | 复用 JA-5~9 Tool 和 Journey 产物；第一版采用同步委派，长任务异步化后置 |
| JA-10.1 | Agent Handoff Contract 与交接校验器 | 每个子 Agent 提交版本化 Artifact + Manifest；结构、来源、确定性、语义、政策和消费者契约全部通过后才启动下一任务 |
| JA-11 | MCP Adapter（需要外部 Agent/客户端调用时） | MCP 与内部 Tool 共享同一应用层实现 |

### 明确非目标

- JA-1~4 不实现自动评论、自动私信或无人确认投递。
- 不把平台 cookie、API key、简历内容放进模型 prompt 或工具返回日志。
- 不直接暴露任意文件读写、shell 执行或任意 URL 下载给 JobAgent。

### 最终多 Agent 目标（非第一阶段）

```text
JobAgent Supervisor
  -> Interview Research Agent
       output: Interview Evidence / Raw Source Snapshot refs / Evidence Gap Report
  -> Interview Preparation Agent
       output: Preparation Pack / question answers / JD focus / candidate gap plan
  -> Tailored Resume Agent
       output: Tailored Resume draft + source-fact provenance
  -> Application Agent
       output: application record (HITL before submission)
  -> Scheduling Agent
       output: availability proposals / HR reply draft / interview calendar record
```

- 子 Agent 之间通过 Opportunity Journey 中的结构化产物 ID 移交，不传递不可审计的自由文本状态。
- 面经中的原题与原答案、模型补充答案必须分别标注；没有可靠答案时不得伪装成来源原文。
- 简历定制只能选择、压缩、排序和措辞优化已确认事实，不得自行补经历、技能、年限或业绩。
- Calendar Adapter 默认只向模型暴露忙闲时间段；读取事件详情需额外授权，给 HR 发消息以及创建、
  修改或接受日历事件必须经过 approve/edit/reject。
- 状态与交接详细设计见 `docs/agent-state-and-handoff.md`；聊天消息和子 Agent 的自然语言完成声明
  都不能直接推进 Journey 或 Workstream 状态。

### 8.1 第一阶段完成记录（2026-08-21）✅

- `discover_interview_evidence(company, role, city?, job_description?)` 是正式 Agent Tool；用户无需
  操作 `xhs-search`/`xhs-download` 参数。
- LLM 根据 JD 生成检索计划；循环根据拒绝理由重新规划，代码负责去重、时效、卖资料风险、
  最大迭代数、证据数量/主题覆盖和停止原因。
- Spider_XHS 详情只请求一次并复用于下载；保存正文、原始响应、全部图片、图片 hash、OCR 文本、
  OCR confidence/engine/version 和去除 xsec token 的 canonical URL。
- 已下载并 OCR 的帖子无论当前 JD 的 Post Relevance Assessment 是否 `REJECTED`，都会进入跨
  Journey 的 `Reusable Source Corpus`；后续搜索再次命中相同 XHS note_id 时复用原始快照，
  仅针对新 JD 重做评估，不重复详情请求、图片下载和 OCR。
- SQLite 保存 Journey、TaskRun、Artifact 状态和 provenance；文件系统保存 immutable snapshot、
  assessment、coverage 及 Markdown/JSON 准备包；LangGraph 对话检查点使用独立 SQLite。
- 相同 `thread-id` 重启后会展示并继续历史对话；长历史采用“较早消息滚动摘要 + 最近消息原文”
  的有界记忆策略，摘要写回同一 LangGraph checkpoint。
- 真实最低预算冒烟：字节跳动后端 JD -> 动态查询 -> 最新 XHS 帖 -> 正文/图片/OCR -> LLM 审查
  -> coverage -> 准备包 -> TaskRun succeeded。样本未达到 A/B 标准时正确输出
  `coverage.sufficient=false`，没有伪造证据。
- 可靠性：LLM 独立 180 秒超时、研究任务总计 300 秒超时，取消会把 TaskRun 标记失败；Markdown
  和 JSON 原子写入，快照缺文件时拒绝物化。
- 验证：`pytest -q` = 127 passed / 1 skipped；`ruff check .`、`mypy jobagent`、
  `git diff --check` 全绿。

### 8.2 第二阶段起点 📋

- 岗位发现（Boss/官网）形成具体 Job/JD，再复用第一阶段面经研究 Tool。
- ReferralDiscovery 找公司员工/内推帖并执行账号真实性与反诈判断；任何评论、私信、简历发送
  都保持 HITL，第一阶段没有实现自动外联。
- 多 Agent 拆分前先实现 Workstream、独立 Validation Report、HandoffManifest 和幂等重试；
  随后再把现有 Tool 分配给 Research/Preparation/Resume/Application/Scheduling 子 Agent。
- Redis/PostgreSQL 限流协调、Milvus/图数据库 RAG 仍是后续演进；第一阶段来源语料使用文件系统
  原始快照 + SQLite 索引，向量库未来只作为可重建检索索引。

### 8.3 本地岗位分析产物与状态板（2026-08-21）✅

- 第一阶段通过 `save_job_analysis` 保存对话中的原始 JD、版本化 Markdown 分析报告和 JSON
  manifest；文件位于 `JOBAGENT_OPPORTUNITY_DIR`，默认 `data/opportunities`。
- 原始 JD 不原地覆盖，同一 Opportunity 的分析使用 `analysis-v001.md`、`analysis-v002.md` 递增。
- manifest 保存公司、岗位、匹配结论、投递状态、SHA-256、文件引用和创建/更新时间。
- 聊天命令 `/status`、`/status week`、`/status month` 分别汇总总体、本周和本月已分析、适合、
  待确认、不适合与已投递岗位；周统计从本地时区周一开始，月统计按自然月。
- `update_job_application_state` 只能更新已存在 Opportunity；没有真实投递结果时不得标记 applied。
- 周/月统计以“最新分析报告生成时间”为准；后续单独更新投递状态不会把历史岗位误计为本周或本月
  新分析。验证基线：`156 passed, 1 skipped`，Ruff、mypy、`git diff --check` 全绿。
- **Future work**：增加稳定 `ArtifactStore` seam 与 S3/MinIO、阿里云 OSS Adapter；数据库仅保存
  Artifact 元数据、版本、哈希和 `storage_uri`。JD/报告、XHS 原图、OCR 输入等大对象迁移到对象
  存储，使用内容寻址去重；本地 manifest 保持可迁移和可回填，不把对象存储作为唯一事实索引。

### 8.4 岗位发现与每周推荐（2026-08-21）🚧

- ✅ R1 启动引导：无 Candidate/Resume 与 Job Search Profile 时主动询问；已有资料只提示缺失项；
  求职画像支持用户确认的公司特征与职位特征，公司规模只是其中一个维度。允许用户直接提供
  JD，避免强制先走推荐流程。验证：
  `157 passed, 1 skipped`，Ruff、mypy、`git diff --check` 全绿。
- 📋 R2 手动触发岗位发现：新增 Job Posting、Job Identity、Job Recommendation Run 持久化；封装
  Boss/公司官网只读 Tool；实现同公司同职位第一版确定性去重、硬约束过滤和匹配排序；增加
  Warm-up/Target/Stretch 策略组合及带依据、可回滚的 Strategy Revision。
- 📋 R3 每周长任务：持久化调度、断点续跑、来源游标、运行锁和周报；重复岗位不重复推荐，发生
  关键 JD/匹配变化时允许重新提醒。
- 📋 R4 投递资格：同公司不同职位不做去重，由独立 Application Eligibility Policy 根据历史投递、
  职位相似度、业务线、公司规则和冷却期返回是否需要人工复核；投递 Agent 不自行发明规则。
  Application Route 支持 Warm-up 的 0–20/20–99 人公司跳过 XHS 内推，按“Boss 匹配 → Tailored
  Resume → 招呼语 → HITL → 联系/投递 → 成功确认”执行；开放自动执行前必须先关闭 CR-02。
- 详细设计：`docs/job-discovery-and-recommendation.md`。

### 8.5 JobAgent 配置目录迁移与 Cookie 健康检查（2026-08-22）✅

- `~/.jobagent` 是唯一主配置目录；所有新 Cookie 写入 `~/.jobagent/cookies/<provider>.json`。
- 启动时仅扫描 `~/.jobagent/cookies` 与兼容目录 `~/.jobclaw/cookies` 下的 JSON，根据 URL、Cookie
  domain 和平台关键 Cookie 自动匹配 Provider，不依赖导出文件名，不递归扫描其他用户目录。
- 从 `~/.jobclaw` 命中后原子复制到 `~/.jobagent`，旧文件保留为可恢复备份；迁移失败不阻断读取。
- 检查浏览器导出的 `expirationDate` 和 Playwright 的 `expires`；过期时只显示 Provider 级提醒，
  不打印 Cookie 名/值，也不阻断聊天和其他来源功能。
- 浏览器扩展 Cookie 在注入 Playwright 前统一规范化：`expirationDate -> expires`，移除
  `sameSite=unspecified` 和扩展私有字段；真实 `jobagent login --platform boss --check` 已通过。
- Boss Job Discovery 改用已验证的只读 JSON Adapter；每实例串行请求、默认最小间隔 1 秒，命中
  风控后本地冷却 900 秒且不自动重试；Tool 返回结构化 blocked 结果，不再让整轮对话崩溃。
- 真实冒烟：Boss Cookie 匹配成功，列表接口首次返回 `code=0` 和 30 个公开岗位；连续诊断后返回
  `code=37`，验证冷却需求。JobAgent 实际调用遇风控后停止，未伪造五角场推荐结果。
- 验证：`168 passed, 1 skipped`；Ruff、mypy、`git diff --check` 全绿。

### 8.6 Boss CDP 接入设计（2026-08-22）📋

- 调研 `mcp-bosszp` 与 `boss-zhipin-scraper`，决定保留 JobAgent Tool/领域层，借鉴后者的 CDP
  被动 Network 捕获、分页、详情、字段映射和风控分类，实现默认 `BossCdpAdapter`。
- 不整体引入 scraper CLI/Hermes Skill；不使用 `mcp-bosszp` 作为运行依赖；`BossHttpAdapter`
  降为默认关闭的实验实现，不在风控后自动切换传输重试。
- 第一阶段先做内部 LangChain Tool；出现真实外部调用方后再增加共享 application module 的薄
  FastMCP Adapter。读操作和 BossApplicationGateway 写操作保持独立 seam。
- 实施顺序：BCDP-1 单页 tracer bullet → BCDP-2 分页/去重 → BCDP-3 完整 JD → BCDP-4 默认
  切换与 Windows 真机验收 → BCDP-5 HITL 投递/MCP。
- 详细设计：`docs/boss-integration-design.md`。

### 8.7 岗位进度登记册与 JD 定制招呼（2026-08-25）✅

- ✅ `jobagent/journey/job_registry.py`：SQLite 岗位登记册（`job_records` + `job_status_events`），
  按稳定岗位身份 `boss:<encryptJobId>` 去重；状态机 `discovered -> recommended -> greeted ->
  hr_replied / no_response -> interviewing -> offer / rejected -> closed`，closed 可重开，
  非法流转抛 `JobTransitionError`。
- ✅ `discover_boss_jobs` 自动入册并给每个岗位标注 `progress_status`/`is_new`/`greeted_at`；
  已打招呼/面试中的岗位 Agent 不再重复推荐；新增 `new_count`/`already_known` 汇总。
- ✅ `boss_greet_jobs` 发送成功自动标 `greeted`；`BossApplier.apply` 支持 per-JD 个性化招呼语
  （`GreetingTarget.greeting` 字段），优先于模板；registry 记录失败不阻断批次。
- ✅ 新 Agent Tools：`update_job_progress` / `get_job_progress`（完整事件历史）/ `list_job_records`
  （按状态/公司筛选，`discovered` 查漏）。
- ✅ System prompt 新增 `<job_progress_policy>`（长周期状态落库、去重、查漏）与
  `<greeting_policy>`（JD 定制招呼语、HITL 确认、禁止模板播报与虚构经历）。
- ✅ `jobagent chat --sessions` 列出历史会话（消息数 + 最后使用时间）；退出时打印恢复命令。
- ✅ `validate-profile` 隐藏为内部批量测试入口；`discover_boss_jobs` 在缺少 Job Search Profile
  或候选人资料时返回 `missing_candidate_data`，Agent 先收集资料再爬取（工具层硬约束）。
- ✅ 测试日志隔离：conftest 将 CLI 入口的 `setup_logging()` 重定向到 `data/logs/tests/`，
  测试 traceback 不再写入 `data/logs/jobagent.log`（含回归测试）。
- 验证基线：`271 passed, 1 skipped`；Ruff、mypy 全绿。

### 8.8 下一阶段功能路线图（2026-08-25 定稿）📋

五个已讨论功能的分阶段交付计划见 **`docs/next-features-roadmap.md`**：

1. F1 岗位进度登记册（✅ 核心已交付；后续：/status 合并视图、投递资格策略、周报）
2. F2 JD 定制打招呼语（✅ 管道已交付；后续：招呼语历史库 + 回复率复盘）
3. F3 定制简历版本库 + Boss 站内简历同步/PDF 上传（TR-1~5，写操作全 HITL）
4. F4 HR 消息流轻量 Gateway（复用 CDP 被动监听，`jobagent watch` 后台进程，HG-1~5）
5. F5 面试录音复盘 + 错题集（IV-1~5，转写/错题本/多维打分/经验贴）

推荐顺序：TR-1/2 -> HG-1/2 -> IV-1/2 -> TR-3/4 -> HG-3/4 -> IV-3~5。

### 8.9 微信 iLink Bot 通道（2026-08-25）🚧

- 调研定稿：微信个人号官方 Bot API（iLink，`ilinkai.weixin.qq.com`，纯 HTTP/JSON，无封号
  风险）；OpenClaw 微信支持即腾讯官方插件走此协议；Hermes 适配器为纯 Python MIT 可合法
  移植。方案：移植核心到 `jobagent/wechat/`，弃 Telegram；Email 仅兜底。
- 关键限制：context_token 机制下 bot 不能冷启动主动推送 -> 通知主通道为桌面 toast（本机
  必达），微信承担双向交互（用户先发消息，bot 回复详情/草稿/确认）。
- ✅ WX-1 已交付：`jobagent/wechat/ilink.py` 文本版客户端（httpx）、QR 扫码登录
  （`jobagent login --platform wechat`，终端 ASCII 二维码）、`--check` token 检查、
  隐藏诊断 `wechat-echo`、`ContextTokenStore`/`MessageDeduplicator`、新依赖 `qrcode`。
- 验证：`288 passed, 1 skipped`；Ruff、mypy 全绿。
- 后续 WX-2 真机验收 -> WX-3 watch 集成 -> WX-4 微信确认流；详见
  `docs/next-features-roadmap.md` F6。
### 8.10 HR Gateway 进程（2026-08-26）🚧

- 设计定稿：`jobagent watch` = gateway 常驻进程，与 `jobagent chat` 仅经 SQLite 通信；
  不通信、无消息总线、无 Node 网关（沿草稿架构）。
- 参考 HermesAgent 本机源码（D:/mashibing/hermes-agent，MIT）核实 iLink 真实协议并移植
  生产模式：QR 状态机（wait/scaned/scaned_but_redirect→redirect_host 换 base_url/
  expired 自动刷新最多 3 次/confirmed 含 ilink_bot_id+ilink_user_id）、getupdates 长轮询
  游标（sync-buf 持久化）、errcode -14 会话过期暂停、连续失败退避+回收会话、内容指纹去重。
- ✅ WX-3-GW 已交付：
  - `jobagent/gateway/wechat_channel.py`：WeChatChannel 轮询回路（会话过期暂停、
    退避、去重、owner 白名单=扫码用户、context_token 持久化）；`RegistryCommandHandler`
    （/status /progress /ping /help，自由文本不回复引导去 chat）。
  - `jobagent watch` 命令；`login --platform wechat` 升级（redirect/刷新/owner）。
  - 删除过渡期 `wechat-echo`（watch 已取代）。
  - 新增验证：`tests/test_wechat_gateway.py` 23 项（MockTransport 零真实网络）。
- 验证：`311 passed, 1 skipped`；Ruff、mypy 全绿。
- 后续：Boss 消息监控（HG-1）接入 watch；桌面 toast；微信确认流（WX-4/HG-6）。
### 8.11 网关设计定稿三件套（2026-08-26，纯设计未开工）📋

- **Boss 三方对话设计**（roadmap F4「Boss 通道三方对话设计」）：多租户隔离单元 =
  job_id；驾驶权状态机 agent_hitl/user_direct；Agent = 无状态起草服务（每次从库重建，
  驾驶权转移免费）；用户手打检测 = 出站消息对账（无 authorization 记录 = 手打）；
  sender 三分（hr/agent_on_behalf/user）；U/M/H 三类 13 个用例矩阵。
- **网关技术选型定稿**（roadmap F4「网关技术选型分析」）：Hermes 分层借鉴——协议层
  已移植，框架层不用（含 proxy 模式/monkey-patch 两条可行但被否路线的完整分析）；
  ChannelAdapter 接口形状对齐 BasePlatformAdapter 保留可逆性。
- **微信 B 模式定稿**（roadmap F6「微信通道模式定稿」）：不省 token，微信 = 完整移动端
  聊天入口；Agent 常驻（wechat:<owner> thread）；实现时删 RegistryCommandHandler
  分发层（其"省 token"前提用户从未要求，已废弃）。
- HG 切片表修订：HG-3 改为驾驶权状态机，HG-2 弃 Telegram 改桌面 toast，HG-5 注记
  watch 骨架已交付；推荐顺序重排。
### 8.12 Agent 所有权模型与中断语义补录（2026-08-26，纯设计）📋

- **Agent 实例所有权模型**（roadmap F6「Agent 实例所有权模型」）：对比 Hermes 网关
  （agent 唯一宿主，AIAgent 实例缓存在网关内存，网关挂 = agent 死）；我们 = 双进程
  各自召唤同一 agent，状态唯一存 SQLite checkpoint，守护边界是进程、状态边界是
  SQLite——chat 与 watch 互不依赖存活的架构依据。
- **中断语义定稿**（roadmap F6「中断语义」）：MVP 采用微信服务器排队（单用户消息
  天然串行不丢，零代码，显式决策非疏漏）；升级路径 = 对话内排队提示（agent 调用改
  后台 task + "还在思考上一条"回复，~50 行），触发条件为实际使用中延迟可感知。
### 8.13 Agent 所有权模型修正（2026-08-27，用户指正）📋

- §8.12 初版含事实错误（"Hermes 状态与网关进程绑死，网关挂 = agent 死"）。源码核实：
  Hermes 消息逐条持久化到 `~/.hermes/state.db`（SQLite），`_agent_cache` 仅是实例性能
  缓存；网关重启自动恢复中断会话（resume_pending + tool-tail 检测 + shutdown flush），
  崩溃恢复比我们当前设计更完善。
- 修正后真实差异只剩拓扑：Hermes 单一常驻网关进程承载所有通道；我们 chat + watch
  两对等进程共享同一 checkpoint DB（chat/watch 互不依赖存活）。
- roadmap F6 已重写该节并附修正声明；后续若 watch 需断点续跑 agent 回合，可借鉴
  Hermes 的标记-扫描-续跑思路。
### 8.14 控制面/数据面分发设计（2026-08-27，借鉴 Hermes slash 命令层）📋

- 研究 Hermes 入口层的结论：其 slash 命令层存在的理由不是省 token，而是控制面/数据面
  分离——/stop 要停的正是运行中的 agent（不能让它自己停自己）；/approve//deny 回答
  agent 正在阻塞等待的问题，路由给 LLM 是循环论证。
- 修正 §8.11 中"删命令分发层"的过度简化：删的是"token 省钱型"分发
  （RegistryCommandHandler）；必须新建"控制面型"分发——WX-4/HG-6 草稿确认流
  （ok/改：xxx/拒）是 outbound_authorizations 的确定性状态转换，可审计，不能进 LLM。
- 分发顺序：待确认草稿→确认词解析（有状态拦截）/ping→pong/其余→agent（B 模式）。
  E1-E6 边界用例入 roadmap F6（代际过期、逐字改写零漂移、FIFO 串行确认等）。
- 与 chat CLI 的关系：同一状态机两个入口（终端确认 + 微信确认），"入口不同，
  终点相同"与 Hermes 同构。
### 8.15 Future Work Track：WX-7 断点续跑 agent 回合（2026-08-27 登记）📋

- 来源：Agent 所有权模型修正（§8.13）时确认 Hermes 崩溃恢复机器比我们 watch 设计
  完善——watch 若在微信对话 ainvoke 中崩溃，checkpoint 留着已完成步骤但无自动续跑，
  用户只能重发。已登记为 F6 正式切片 WX-7。
- 设计要点（借鉴 Hermes startup restore）：崩溃前标记（回合起止写 pending 标记，类比
  handoff_state='resume_pending'）→ 重启扫描标记会话 → 从 checkpoint 尾部续跑（含
  tool-tail 检测：末条是 tool result 而 agent 未回复则补跑该步）→ shutdown flush
  （未发消息落盘）。Hermes 参考位置：run.py startup restore / resume_pending /
  tool-tail / flush_pending_to_file。
- 触发条件（不立即实施）：WX-B 上线使用后，崩溃导致会话中断实际发生且重发体验
  可感知时再动工。
