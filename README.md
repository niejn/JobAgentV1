[English](./README_EN.md)

```
     _       _       _                    _   
    | | ___ | |__   / \   __ _  ___ _ __ | |_ 
 _  | |/ _ \| '_ \ / _ \ / _` |/ _ \ '_ \| __|
| |_| | (_) | |_) / ___ \ (_| |  __/ | | | |_ 
 \___/ \___/|_.__/_/   \_\__, |\___|_| |_|\__|
                         |___/                
```

# JobAgent — AI 求职旅程 Agent：帮你跑腿，你做决定

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

> **一句话说清楚：** JobAgent 是一个开源的 AI 求职 Agent——围绕"一个岗位的完整求职旅程"工作：
> 自动发现岗位并去重入册、检索小红书真实面经生成面试准备包、定制打招呼语并**经你确认后**发送、
> 全程追踪每个岗位的进度。高风险操作一律人工确认（HITL），它是副驾驶，不是无人投递机。
> 你要做的，是做决策和去面试。

---

## 😩 痛点：求职为什么是一个黑洞？

每个找过工作的人都懂：

- **刷岗位像无底洞**：Boss直聘刷到手酸，投没投过、聊没聊过全靠记忆，重复打招呼很尴尬
- **面经散落小红书**：想准备某家公司的面试，搜索、翻帖、截图、OCR，一套下来一晚上没了——
  结果发现贴子里全是"求捞"和广告
- **看完面经也不知道准备啥**：别人的面经是流水账，哪些是这个岗位真会考的？没人帮你归纳
- **投完就忘**：哪家回复了？哪家约面了？哪家其实已经凉了？Excel 顶着，但没人记得更新
- **打招呼千篇一律**："你好，我很感兴趣" 发一百遍，HR 看都不看

你的时间应该花在**准备面试和提升自己**上，不是当人肉检索器和进度追踪器。

---

## 🤖 JobAgent 帮你做什么？

JobAgent 不再是"一键躺平批量投递"的脚本，而是围绕 **Opportunity Journey（岗位求职旅程）**
的对话式 Agent。对每一个"公司 × 岗位"，它陪你走完从发现到复盘的全程：

| 环节 | 人工操作 | JobAgent |
| --- | --- | --- |
| 🔍 发现岗位 | 多平台刷到眼花，重复岗位来回看 | **Boss 岗位发现** + 稳定身份去重，自动入册 |
| 📇 进度追踪 | Excel？备忘录？全靠脑子？ | **岗位进度登记册**：状态机 + 全量流转历史，`/status` 一屏看清 |
| 📚 面经调研 | 小红书手翻 + 截图 + 人肉 OCR | **面经证据研究循环**：检索、下载、OCR、证据分级全自动 |
| 🎯 准备面试 | 流水账面经无从下手 | **面试准备包**：Markdown + JSON，题目、能力矩阵、薄弱点、学习计划 |
| 💬 打招呼 | 复制粘贴同一句话发一百遍 | **JD 定制打招呼语**，发送前必须经你确认 |
| 🔔 消息跟进 | 睡前挨个 App 翻 HR 回复 | **Telegram / Discord 通知** + 微信 HR Gateway（实验性） |

**简单说：你用自然语言告诉它目标公司、岗位和 JD，它跑腿干活、汇报进展；该你拍板的地方，
它一定会先问你。**

---

## ✨ 核心功能

### 💬 对话式 Agent（主入口）

```bash
jobagent chat --config profiles/agent.yaml
```

自然语言描述公司、岗位和 JD，例如"帮我准备字节跳动北京后端岗位的面试，JD 是……"。
Agent 会自己选择业务 Tool 干活：发现岗位、检索面经、分析 JD、登记进度……你全程看得见它
调用了什么工具。会话持久化在 SQLite（`--session-id` 可恢复），消息达到阈值后自动生成滚动
摘要，长对话也不会失忆。聊天内可用 `/status` 查看岗位看板、`/sessions` 切换会话。

**第一版统一使用 OpenAI-compatible 接口：** 配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`
和 `JOBAGENT_LLM_MODEL`，即可接入 DeepSeek、火山方舟、OpenAI 等兼容服务。

### 📚 面经证据研究循环

不是"搜索一下让 LLM 编个总结"，而是可追溯的证据工程：

1. **检索计划**：按证据缺口规划搜索词，随上一轮结果质量持续修订
2. **原始快照**：小红书帖子的正文、图片、作者、时间原样入库，不可变更
3. **图片内容提取**：本地 OCR 提取图片文字，记录引擎与置信度
4. **相关性判定 + 证据准入**：只允许 **A 级**（同公司同岗位）和 **B 级**（同公司相近岗位）
   面经进入准备包，其他公司的"通用面经"混不进来
5. **证据缺口报告**：目标公司没有可用 A/B 证据时，明确报告缺口，绝不把推断编成真实面经
6. **可复用语料库**：帖子原始快照跨岗位持久保存，下次遇到同一帖子只重新评估，不重复下载

### 📇 岗位进度登记册

每个岗位从 `discovered → recommended → greeted → hr_replied / interviewing → offer / rejected → closed`
的状态流转全部落 SQLite，非法流转直接报错，不静默漂移。打招呼成功自动标记；已打过招呼或
面试中的岗位不再重复推荐；HR 迟来回复还能重开已关闭的记录。

### 💬 Boss 打招呼（HITL）

Boss直聘没有公开投递 API。JobAgent 用 **Playwright 真实浏览器 + CDP** 复现投递流程，
并根据 JD 定制打招呼语——但**每一次发送批次都必须先征得你的明确确认**，这是写死在 Tool
描述里的硬约束，不是君子协定。另有随机延迟、每日上限、僵尸岗过滤、防重复投递、验证码检测
暂停 + 通知等防封策略。

### 🍪 Cookie 管理

- `jobagent login` — 弹出浏览器，交互式登录，cookie 自动保存到 `~/.jobagent/cookies/`
- `jobagent login --check` — 检查已保存的 cookie 是否还有效
- 支持 Boss直聘 + LinkedIn + 小红书

### 🔔 通知与消息网关

- **Telegram Bot** — 配置 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- **Discord Webhook** — 配置 `DISCORD_WEBHOOK_URL`
- **微信 HR Gateway（实验性）** — `jobagent watch` 启动微信机器人，接收 HR 消息并响应岗位进度指令

---

## 🚀 快速开始

### 1. 克隆 & 安装

```bash
git clone https://github.com/VPC-byte/jobagent.git
cd jobagent
pip install -e .
```

### 2. 安装浏览器内核

```bash
playwright install chromium
```

### 3. 登录求职平台

```bash
# 登录小红书（面经研究必需：弹浏览器扫码/登录，cookie 存本地）
jobagent login --platform xhs

# 登录 LinkedIn（可选）
jobagent login --platform linkedin

# 登录微信（HR Gateway 必需：终端里扫码，不开浏览器）
jobagent login --platform wechat

# 检查 cookie 是否仍有效（任一平台）
jobagent login --platform xhs --check

# 一次性登录全部支持的平台
jobagent login --platform all
```

> 💡 **Boss直聘不需要 `login`**：它通过 CDP 连接你本机已登录的真实 Chrome，
> 确保 Chrome 以远程调试端口启动、且你已在其中登录 Boss 即可。

### 4. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，根据需要填写（详见下方[配置说明](#-配置说明)）。

### 5. 配置 Agent 启动上下文

```bash
cp profiles/search.example.yaml profiles/search.yaml
cp profiles/background.example.yaml profiles/background.yaml
cp profiles/agent.example.yaml profiles/agent.yaml
```

`search.yaml` 是期望岗位、城市和约束；`background.yaml` 是从简历提取并由你确认的经历、
项目和技能。两者不是同一种 Profile。`agent.yaml` 只引用它们，并支持自动加载 UTF-8
Markdown 简历；PDF 解析列入后期能力。

### 6. 开跑！

```bash
# 启动对话式 Agent：自然语言描述公司、岗位和 JD
jobagent chat --config profiles/agent.yaml
```

例如输入"帮我准备字节跳动北京后端岗位的面试，JD 是……"。Agent 会动态检索近期小红书
面经，保存正文和图片，执行 OCR 与证据准入，并生成 Markdown + JSON 准备包。面经研究
是只读流程；打招呼等写操作一律先请求你确认。

---

## 🔧 CLI 命令全集

### `jobagent chat` — 对话式 Agent（主入口）

```bash
# 启动新会话
jobagent chat --config profiles/agent.yaml

# 恢复已有会话
jobagent chat --config profiles/agent.yaml --session-id <会话 ID>

# 列出所有历史会话
jobagent chat --sessions
```

聊天内命令：`/status` 查看岗位进度看板，`/sessions` 列出并切换会话，`/exit` 退出。

### `jobagent login` — 登录平台

```bash
jobagent login --platform boss
jobagent login --platform linkedin
jobagent login --platform boss --check   # 只校验 cookie，不弹浏览器
```

### `jobagent watch` — 微信 HR Gateway（实验性）

```bash
# 启动微信机器人 + 岗位进度指令处理
jobagent watch --channel wechat
```

### `jobagent scrape` — 只抓取，不投递

```bash
# 抓取 Boss直聘上的岗位，先看看质量
jobagent scrape --platform boss --query "后端工程师" --limit 20

# 全平台抓取
jobagent scrape --platform all --query "Python 开发" --limit 30
```

### `jobagent run` — 早期批量流程（保留）

```bash
jobagent run --platform boss --profile profiles/me.yaml --query "大模型工程师" --limit 20
```

> 💡 日常使用推荐从 `chat` 入口开始，Agent 会按旅程协调各业务 Tool；`scrape` / `run`
> 保留用于调试和单点执行。

---

## ⚙️ 配置说明

### `.env` 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| **核心运行时** | | | |
| `JOBAGENT_ENV` | 否 | `development` | 运行环境 |
| `JOBAGENT_LOG_LEVEL` | 否 | `INFO` | 日志级别 |
| `JOBAGENT_HEADLESS` | 否 | `true` | 浏览器是否无头模式（调试时设 `false` 看操作过程） |
| `JOBAGENT_MAX_JOBS` | 否 | `30` | 单次最大处理岗位数 |
| `JOBAGENT_REQUEST_TIMEOUT` | 否 | `30` | 网络请求超时（秒） |
| `JOBAGENT_HISTORY_COMPACT_AFTER_MESSAGES` | 否 | `40` | 达到该消息数时压缩较早历史 |
| `JOBAGENT_HISTORY_KEEP_RECENT_MESSAGES` | 否 | `16` | 压缩后保留的最近完整消息数 |
| `JOBAGENT_HISTORY_SUMMARY_INPUT_MAX_CHARS` | 否 | `24000` | 单次摘要读取的历史文本字符上限 |
| `JOBAGENT_HISTORY_SUMMARY_TIMEOUT` | 否 | `60` | 历史摘要模型调用超时（秒） |
| **LLM 配置** | | | |
| `JOBAGENT_LLM_PROVIDER` | 否 | `openai-compatible` | 第一版仅支持 OpenAI-compatible |
| `JOBAGENT_LLM_MODEL` | 否 | `gpt-4o-mini` | 当前 provider 的模型名或火山方舟 Endpoint/Model ID |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI-compatible 服务地址，可配置 DeepSeek、火山方舟等 |
| `OPENAI_API_KEY` | OpenAI-compatible 时是 | - | 对应服务的 API Key |
| **平台 Cookie** | | | |
| `BOSS_COOKIE` | 否 | - | Boss直聘 cookie（优先于持久化文件） |
| `LINKEDIN_COOKIE` | 否 | - | LinkedIn cookie |
| **Boss直聘专属** | | | |
| `BOSS_GREETING` | 否 | - | 打招呼模板，支持 `$company` `$title` `$name` 变量 |
| `BOSS_APPLY_DELAY_MIN` | 否 | `3.0` | 投递间隔最小秒数 |
| `BOSS_APPLY_DELAY_MAX` | 否 | `8.0` | 投递间隔最大秒数 |
| `BOSS_DAILY_LIMIT` | 否 | `100` | 每日投递上限（1-150） |
| `BOSS_SKIP_INACTIVE_DAYS` | 否 | `7` | 跳过 HR 多少天未活跃的岗位 |
| **小红书只读采集** | | | |
| `XHS_API_RATE_REQUESTS` | 否 | `1` | 一个限流周期内允许的搜索、详情等 API 调用数 |
| `XHS_API_RATE_PERIOD_SECONDS` | 否 | `1` | API 限流周期（秒）；默认即最多每秒 1 次 |
| `XHS_MEDIA_RATE_REQUESTS` | 否 | `1` | 一个限流周期内允许的图片下载数 |
| `XHS_MEDIA_RATE_PERIOD_SECONDS` | 否 | `1` | 图片限流周期（秒）；默认即最多每秒 1 张 |
| **通知** | | | |
| `TELEGRAM_BOT_TOKEN` | 否 | - | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | 否 | - | Telegram Chat ID |
| `DISCORD_WEBHOOK_URL` | 否 | - | Discord Webhook URL |
| **网络** | | | |
| `HTTP_PROXY` | 否 | - | HTTP 代理 |
| `HTTPS_PROXY` | 否 | - | HTTPS 代理 |

> Claude OAuth/Anthropic 不属于第一版模型通道，后续在完成 Agent tool-call 适配后再接入。

小红书限流由 `pyrate-limiter` 提供，当前使用进程内存桶，不依赖 Redis。搜索、详情、作者帖子
和初始化走 API 桶；帖子图片逐张取得媒体桶许可。以后多实例部署时可沿用同一适配接口换成
Redis/PostgreSQL 桶，无需把固定 `sleep` 散落到研究工作流中。

### `profiles/search.yaml` 求职画像

描述你长期确认的目标与约束，Agent 不会静默改写它：

```yaml
desired_roles:
  - AI Agent 开发工程师
  - 后端开发（AI 方向）
preferred_locations:
  - "北京"
  - "上海"
remote_ok: true
preferred_industries:
  - AI
preferred_company_sizes:
  - growth
  - large
constraints:
  - Do not apply without human confirmation   # ← 我们的态度，也是默认行为
```

`profiles/background.yaml` 则是从简历提取、经你确认的经历与技能；`profiles/agent.yaml`
引用以上两者并指向你的简历文件。

---

## 🏗️ 架构

```text
+---------------------------------------------------------------+
|                     jobagent chat（对话入口）                    |
|        会话持久化 + 滚动摘要 / 人工确认（HITL）边界                |
+-------------------------------+-------------------------------+
                                |
                                v
+-------------------------------+-------------------------------+
|                     Agent 业务 Tool 层                          |
|  岗位发现  面经证据研究  JD 分析  打招呼(HITL)  进度登记  简历画像  |
+------+----------------+---------------+---------------+--------+
       |                |               |               |
       v                v               v               v
+-----------+  +---------------+  +------------+  +-------------+
| Playwright |  | 小红书只读采集 |  | Boss 真实   |  |   SQLite    |
| / CDP 浏览器|  | 快照+OCR+限流  |  | 浏览器操作  |  | 登记册/语料库 |
+-----------+  +-------+-------+  +-----+------+  +------+------+
                       |                |                |
                       v                v                v
              +-------------------------------------------------+
              |        通知层：Telegram / Discord / 微信 Gateway    |
              +-------------------------------------------------+
```

**关键边界：**

- **证据可追溯**：准备包中每条面经都能回溯到原始快照（正文 + 图片 + 作者 + 时间）
- **高风险操作 HITL**：发送简历、打招呼、对外写操作必须经用户确认；第一阶段面经研究纯只读
- **真实浏览器采集**：不用 TLS 指纹模拟库，统一走 Playwright / CDP 真实浏览器

## 📂 项目结构

```text
jobagent/
  agent.py                 # 对话式 Agent（LangGraph + 流式输出）
  cli.py                   # CLI 入口（Click）：chat / login / watch / scrape / run
  config.py                # 配置管理（pydantic-settings）
  domain.py                # 数据模型（Pydantic）
  skills.py                # Skill 装载（纯文档、跨 Agent 可移植）
  tools/                   # Agent 业务 Tool 层
    boss_greet.py          #   Boss 打招呼（发送前强制 HITL 确认）
    job_discovery.py       #   岗位发现 + 自动入册去重
    job_progress.py        #   岗位进度登记册读写
    interview_evidence.py  #   面经证据研究循环
    job_description.py     #   JD 分析
    candidate_profile.py   #   求职画像 / 候选人背景
    shared_url.py          #   分享链接提取与保存
    xhs_note.py            #   小红书帖子保存
    xhs_author.py          #   作者帖子检索
  interview/               # 面经研究引擎
    research.py            #   检索计划 + 研究循环
    snapshot.py            #   Raw Source Snapshot 原始快照
    source_corpus.py       #   可复用来源语料库
    ocr.py                 #   图片内容提取（本地 OCR）
    intelligence.py        #   相关性判定与证据准入
  journey/                 # Opportunity Journey 持久层
    job_registry.py        #   岗位进度登记册（状态机 + 事件历史）
    store.py               #   会话 / 产物存储
  gateway/                 # 微信 HR Gateway（实验性）
  applier/                 # Boss / LinkedIn 投递器（防封策略 + 验证码处理）
  auth/                    # 浏览器登录 + Cookie 持久化
  matcher/                 # LLM 匹配引擎
  notifier/                # Telegram / Discord 通知
  scraper/                 # Boss / LinkedIn 爬虫
profiles/
  search.example.yaml      # 求职画像模板（目标与约束）
  background.example.yaml  # 候选人背景模板（经历与技能）
  agent.example.yaml       # Agent 启动配置模板
docs/
  architecture.md          # 架构设计文档
  next-features-roadmap.md # 功能路线图
tests/
```

## 📋 支持平台

| 平台 | 能力 | 说明 |
| --- | --- | --- |
| **Boss直聘** (zhipin.com) | ✅ 岗位发现 / 打招呼（HITL） | Playwright + CDP 真实浏览器 |
| **小红书** | ✅ 面经研究（只读） | 快照 + OCR + 证据分级，不评论不私信 |
| **LinkedIn** | ✅ 抓取 | Easy Apply 列入后期规划 |
| **微信** | 🚧 HR Gateway（实验性） | `jobagent watch` |
| 拉勾 / 前程无忧 | 🔜 | 适配器开发中 |

---

## 🤝 参与贡献

欢迎 PR！特别欢迎：

- 🔌 **新平台适配器**（拉勾、前程无忧、猎聘……）
- 🧠 **更好的证据分级与检索策略**（证据准入规则、覆盖度评估）
- 🔒 **稳定性改进**（反爬对抗、断点续跑、限流策略）
- 📢 **新通知渠道**（飞书、钉钉）

```bash
pip install -e .[dev]
pytest -q
```

提 PR 前请跑测试、加类型标注、更新相关文档。

## ⚠️ 合规声明

自动化操作求职平台可能受到平台条款和当地法律的限制。JobAgent 的高风险写操作一律要求
人工确认，但请在合法合规、个人授权范围内使用本项目。作者不对滥用行为承担责任。

## 📜 许可证

MIT License — 详见 [LICENSE](./LICENSE)。


  后端服务：

  uvicorn jobagent.web:app --reload --port 8008

  后端地址：

  http://127.0.0.1:8008

  前端服务：

  npm install
  npm run dev

  前端地址通常是：

  http://localhost:5173

  前端会把这些请求代理到后端：

  /api      -> http://127.0.0.1:8008
  /healthz  -> http://127.0.0.1:8008

  命令行 Agent：

  jobagent chat

  其他已有命令：

  jobagent login --platform xhs
  jobagent login --platform boss
  jobagent watch
  jobagent xhs-search --help

  总结：

  后端：uvicorn jobagent.web:app --reload --port 8008
  前端：npm run dev
  Agent：jobagent chat

mitmdump -p 8080 -w .ua\resume_request_accept.mitm