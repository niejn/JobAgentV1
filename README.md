[English](./README_EN.md)

```
       __      __    ________
      / /___  / /_  / ____/ /___ __      __
 __  / / __ \/ __ \/ /   / / __ `/ | /| / /
/ /_/ / /_/ / /_/ / /___/ / /_/ /| |/ |/ /
\____/\____/_.___/\____/_/\__,_/ |__/|__/
```

# JobAgent — AI 帮你投简历，你只管躺平

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

> **一句话说清楚：** JobAgent 是一个开源的 AI 求职 Agent——自动抓岗位、用大模型帮你匹配、一键批量投递，投完还给你发 Telegram/Discord 消息汇报。
> 你要做的，就是写好简历，然后等面试通知。

---

## 😩 痛点：求职为什么这么累？

每个找过工作的人都懂：

- **Boss直聘刷到手酸**，一个个点进去看 JD，看完觉得不太行，退出来继续刷
- 好不容易看到一个还行的，点"立即沟通"，想半天打招呼说啥，发完又没下文
- 一天下来投了十几个，回复的寥寥无几，**时间全花在重复劳动上**
- 有些岗位 HR 三个月没上线了，你还在那认真写打招呼语——**投了个寂寞**
- LinkedIn 也要刷，拉勾也要看，多平台来回切，脑子都乱了
- 投完之后**石沉大海**，哪个回了、哪个没回，全靠记忆

你的时间应该花在**准备面试和提升自己**上，不是当人肉投递机器。

---

## 🦀 JobAgent 帮你做什么？

| 步骤 | 人工操作 | JobAgent |
| --- | --- | --- |
| 🔍 搜岗位 | 多平台来回切，关键词一个个试 | **自动抓取** Boss直聘 + LinkedIn |
| 📖 看 JD | 一个个点开，人肉阅读判断匹不匹配 | **LLM 智能匹配**，打分 + 给出匹配理由 |
| 💬 打招呼 | 想措辞、复制粘贴、一个个发 | **自动生成打招呼语**，支持模板变量 |
| 📤 投简历 | 点「立即沟通」重复 100 遍 | **自动批量投递**高匹配岗位 |
| 🧹 过滤垃圾 | 全靠直觉，经常投到僵尸岗 | **HR 活跃度过滤**，跳过僵尸岗 |
| 📊 跟进状态 | Excel？备忘录？全靠脑子？ | **Telegram / Discord 实时通知** |

**简单说：你配好简历和偏好，JobAgent 帮你 24 小时无休投递。**

---

## ✨ 核心功能

### 🤖 LLM 智能匹配引擎

不是简单的关键词匹配，而是用大模型理解你的背景和岗位要求，给出**匹配分数 + 匹配理由**。

**第一版统一使用 OpenAI-compatible 接口：** 配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`
和 `JOBAGENT_LLM_MODEL`，即可接入 DeepSeek、火山方舟、OpenAI 等兼容服务。Claude
OAuth/Anthropic 列入后期能力，不进入第一版运行路径。

### 📮 Boss直聘自动投递（核心卖点）

这不是简单的 API 调用——Boss直聘没有公开投递 API。JobAgent 用 **Playwright 模拟真人浏览器操作**，完整复现投递流程：

1. 打开岗位页面 → 点击"立即沟通"
2. 输入打招呼消息 → 点击发送
3. 等待随机延迟 → 下一个

**防封策略：**

- ⏱️ **随机延迟**：每次投递间隔 3-8 秒（可配置），模拟人类节奏
- 📅 **每日上限**：默认 100 次/天，避免触发风控
- 👻 **僵尸岗过滤**：跳过 HR 超过 N 天未活跃的岗位（默认 7 天）
- 🔄 **防重复投递**：JSON 历史记录，已沟通过的自动跳过
- 🤖 **验证码检测**：遇到验证码自动暂停，发 Telegram 通知你人工处理

**打招呼消息模板：**

```
你好！我对贵公司 $title 岗位很感兴趣，$name，方便聊聊吗？
```

支持变量：`$company`（公司名）、`$title`（岗位名）、`$name`（HR 名字）

### 🍪 Cookie 管理

- `jobagent login` — 弹出浏览器，交互式登录，cookie 自动保存到 `~/.jobagent/cookies/`
- `jobagent login --check` — 检查已保存的 cookie 是否还有效
- **优先级**：`.env` 中配置的 cookie > 持久化文件 > 提示你重新 login
- 支持 Boss直聘 + LinkedIn

### 🔔 通知

投递完成后自动汇报：

- **Telegram Bot** — 配置 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- **Discord Webhook** — 配置 `DISCORD_WEBHOOK_URL`

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
# 登录 Boss直聘（弹出浏览器，手动扫码/登录）
jobagent login --platform boss

# 登录 LinkedIn
jobagent login --platform linkedin

# 一次性登录所有平台
jobagent login --platform all

# 检查 cookie 是否有效
jobagent login --platform boss --check
```

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
项目和技能。两者不是同一种 Profile。`agent.yaml` 只引用它们；第一阶段暂不自动解析原始简历。

### 6. 开跑！

```bash
# 启动单 Agent：自然语言描述公司、岗位和 JD
jobagent chat --config profiles/agent.yaml
```

例如输入“帮我准备字节跳动北京后端岗位的面试，JD 是……”。Agent 会动态检索近期小红书
面经，保存正文和图片，执行 OCR 与证据准入，并生成 Markdown + JSON 准备包。第一阶段是
只读研究流程，不会自动评论、私信、发送简历或投递。

---

## 🔧 CLI 命令全集

### `jobagent login` — 登录平台

```bash
# 登录 Boss直聘
jobagent login --platform boss

# 登录 LinkedIn
jobagent login --platform linkedin

# 登录所有支持的平台
jobagent login --platform all

# 设置登录超时（分钟）
jobagent login --platform boss --timeout 5

# 检查 cookie 有效性（不弹浏览器）
jobagent login --platform boss --check
```

### `jobagent scrape` — 只抓取，不投递

```bash
# 抓取 Boss直聘上的岗位
jobagent scrape --platform boss --query "后端工程师" --limit 20

# 抓取 LinkedIn
jobagent scrape --platform linkedin --query "AI Engineer" --limit 10

# 全平台抓取
jobagent scrape --platform all --query "Python 开发" --limit 30
```

> 💡 先用 `scrape` 看看抓到的岗位质量，再决定要不要跑完整流程。

### `jobagent run` — 完整流程

```bash
# 基础用法
jobagent run --profile profiles/me.yaml --query "AI 工程师"

# 指定平台 + 限制数量
jobagent run --platform boss --profile profiles/me.yaml --query "大模型工程师" --limit 20

# 全平台
jobagent run --platform all --profile profiles/me.yaml --query "后端开发" --limit 50
```

### `jobagent validate-profile` — 校验画像文件

```bash
jobagent validate-profile --profile profiles/me.yaml
```

确认你的 YAML 格式没问题再跑，避免跑到一半报错。

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
和初始化走 API 桶；帖子图片在复用 Spider_XHS 下载函数的同时逐张取得媒体桶许可。以后多实例部署
时可沿用同一适配接口换成 Redis/PostgreSQL 桶，无需把固定 `sleep` 散落到研究工作流中。

`jobagent chat` 使用 `--session-id` 恢复 SQLite 中的同一会话。启动时会显示已恢复的近期对话；
消息达到阈值后，较早内容会生成滚动摘要，最近消息保留原文。摘要与近期消息都会继续参与后续回答。

已成功下载并完成 OCR 的小红书帖子会进入跨岗位的 `Reusable Source Corpus`。帖子即使不符合
当前公司/JD、被相关性评估拒绝，原始正文、图片、OCR、作者、时间和来源仍会保留；后续 JD
再次遇到同一帖子时只重新评估相关性，不重复下载和 OCR。第一阶段使用 SQLite 索引和文件系统
原始快照；后续可以增加全文、Milvus 或其他向量索引，但这些索引不能替代原始资料。

### `profiles/me.yaml` 求职画像

```yaml
name: "张三"
email: "zhangsan@example.com"
years_experience: 3

summary: >
  全栈开发，擅长 AI/ML 应用、云基础设施和 DevOps。
  目前在找 AI Agent 相关的岗位。

skills:
  - Python
  - TypeScript
  - LLM/Agent 开发
  - Kubernetes
  - Docker
  - AWS/GCP

desired_roles:
  - AI Agent 开发工程师
  - 大模型应用工程师
  - 后端开发（AI 方向）

preferences:
  locations:
    - "深圳"
    - "remote"
  salary_min: 25000
  salary_max: 50000
  work_schedule: "双休"
  industries:
    - AI
    - Web3
    - Cloud
  deal_breakers:
    - "996"
    - "大小周"
    - "无社保"
```

---

## 🏗️ 架构

```text
+---------------------+
|   Profile Loader    |  你的简历 / 求职画像 (YAML)
+----------+----------+
           |
           v
+---------------------+      +----------------------+
|   Scraper Layer     |----->| Unified Job Objects  |
| Boss直聘 / LinkedIn |      | (Pydantic 标准化)     |
+----------+----------+      +----------+-----------+
           |                            |
           v                            v
+---------------------+      +----------------------+
|   LLM Matcher       |----->| 匹配分数 + 匹配理由   |
| Claude OAuth/API/   |      | (SSE streaming)      |
| OpenAI              |      +----------+-----------+
+----------+----------+                 |
           |                            v
+---------------------+      +----------------------+
|  Auto Applier       |----->| 投递状态              |
| Boss直聘 / LinkedIn |      | 已投 / 失败 / 跳过    |
+----------+----------+      +----------+-----------+
           |                            |
           +------------+---------------+
                        v
              +--------------------+
              | Notification Layer |
              | Telegram / Discord |
              +--------------------+
```

## 📂 项目结构

```text
jobagent/
  applier/                 # 自动投递
    base.py                #   投递器基类
    boss.py                #   Boss直聘投递（打招呼 + 防封策略）
    linkedin.py            #   LinkedIn Easy Apply
    captcha.py             #   验证码检测 + 通知
    history.py             #   投递历史记录（防重复）
  auth/                    # 登录认证
    browser_login.py       #   Playwright 交互式浏览器登录
    cookie_manager.py      #   Cookie 持久化管理
    claude_auth.py         #   Claude OAuth 凭证读取
    token_refresh.py       #   OAuth token 自动刷新
  matcher/                 # LLM 匹配引擎
    llm_matcher.py         #   多 provider 匹配打分
  models/                  # Claude API 客户端
    claude_api.py          #   Claude API 封装
    streaming.py           #   SSE streaming + retry/backoff
  notifier/                # 通知
    telegram.py            #   Telegram Bot 通知
    discord.py             #   Discord Webhook 通知
  profile/                 # 画像加载
    loader.py              #   YAML 画像解析
  scraper/                 # 爬虫
    base.py                #   爬虫基类
    boss.py                #   Boss直聘抓取
    linkedin.py            #   LinkedIn 抓取
  cli.py                   # CLI 入口（Click）
  config.py                # 配置管理（pydantic-settings）
  domain.py                # 数据模型（Pydantic）
profiles/
  example.yaml             # 画像模板
docs/
  architecture.md          # 架构设计文档
tests/
  test_models.py           # 测试
```

## 📋 支持平台

| 平台 | 抓取 | 投递 | 说明 |
| --- | --- | --- | --- |
| **Boss直聘** (zhipin.com) | ✅ | ✅ | Playwright 模拟打招呼 |
| **LinkedIn** | ✅ | ✅ | Easy Apply 自动投递 |
| 拉勾 (Lagou) | 🔜 | 🔜 | 适配器开发中 |
| 前程无忧 (51Job) | 🔜 | 🔜 | 适配器开发中 |

---

## 🤝 参与贡献

欢迎 PR！特别欢迎：

- 🔌 **新平台适配器**（拉勾、前程无忧、猎聘……）
- 🧠 **更好的匹配策略**（prompt 调优、多维度评分）
- 🔒 **稳定性改进**（反爬对抗、断点续跑）
- 📢 **新通知渠道**（微信、飞书、钉钉）

```bash
pip install -e .[dev]
pytest -q
```

提 PR 前请跑测试、加类型标注、更新相关文档。

## ⚠️ 合规声明

自动化操作求职平台可能受到平台条款和当地法律的限制。请在合法合规、个人授权范围内使用本项目。作者不对滥用行为承担责任。

## 📜 许可证

MIT License — 详见 [LICENSE](./LICENSE)。
