[中文](./README.md)

```
       __      __    ________
      / /___  / /_  / ____/ /___ __      __
 __  / / __ \/ __ \/ /   / / __ `/ | /| / /
/ /_/ / /_/ / /_/ / /___/ / /_/ /| |/ |/ /
\____/\____/_.___/\____/_/\__,_/ |__/|__/
```

# JobAgent — Stop Applying Manually. Let AI Do the Grunt Work.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

> **TL;DR:** JobAgent is an open-source AI job hunting agent. It scrapes job listings from LinkedIn and Boss直聘, matches them against your profile using LLMs, auto-applies to the best fits, and notifies you of results via Telegram or Discord.
> You write the resume. JobAgent does the rest.

---

## 😩 The Problem

Job hunting is a grind. Everyone knows it:

- You scroll through **LinkedIn** for hours, tweaking keywords, opening tabs
- You read each JD manually to decide if it's worth applying
- You click "Easy Apply" dozens of times a day — same info, over and over
- Half the postings are ghost jobs where the recruiter hasn't logged in for months
- You lose track of what you applied to, what ghosted you, what's pending

Your time is better spent **preparing for interviews and building skills** — not being a human apply-bot.

---

## 🦀 What JobAgent Does

| Step | Manual | JobAgent |
| --- | --- | --- |
| 🔍 Find jobs | Jump between platforms, try different keywords | **Auto-scrape** LinkedIn + Boss直聘 |
| 📖 Evaluate fit | Read each JD, gut-feel it | **LLM-powered matching** with scores + explanations |
| 💬 Craft messages | Write custom intros for each role | **Auto-generated greetings** with template variables |
| 📤 Apply | Click "Apply" 100 times | **Auto-apply** to high-match roles |
| 🧹 Filter junk | Pure intuition, often wasted on ghost jobs | **Recruiter activity filter** — skip inactive postings |
| 📊 Track status | Spreadsheet? Sticky notes? Memory? | **Real-time Telegram / Discord notifications** |

**Set up your profile once. Let JobAgent work 24/7.**

---

## ✨ Key Features

### 🤖 LLM-Powered Matching Engine

Not simple keyword matching — JobAgent uses large language models to understand your background and job requirements, producing **match scores with explanations**.

**The first release uses one OpenAI-compatible interface.** Configure `OPENAI_BASE_URL`,
`OPENAI_API_KEY`, and `JOBAGENT_LLM_MODEL` for DeepSeek, Volcengine Ark, OpenAI, or another
compatible provider. Claude OAuth/Anthropic support is deferred.

### 📮 Boss直聘 Auto-Apply (China Market)

Boss直聘 has no public application API. JobAgent uses **Playwright browser automation** to simulate the full human interaction flow:

1. Open job page → Click "Start Chat"
2. Type greeting message → Send
3. Random delay → Next job

**Anti-detection measures:**

- ⏱️ **Random delays**: 3-8 seconds between actions (configurable)
- 📅 **Daily cap**: Default 100 applications/day to avoid rate limits
- 👻 **Ghost job filter**: Skip postings where HR hasn't been active for N days (default: 7)
- 🔄 **Dedup**: JSON history prevents re-applying to the same job
- 🤖 **CAPTCHA detection**: Auto-pauses and sends Telegram alert for manual intervention

**Greeting template:**

```
Hi! I'm very interested in the $title role at $company, $name. Would love to chat!
```

Variables: `$company`, `$title` (job title), `$name` (recruiter name)

### 🍪 Cookie Management

- `jobagent login` — Opens browser for interactive login, cookies auto-saved to `~/.jobagent/cookies/`
- `jobagent login --check` — Verify saved cookies are still valid
- **Priority**: `.env` config > persisted files > prompt to re-login
- Supports Boss直聘 + LinkedIn

### 🔔 Notifications

Auto-report after each run:

- **Telegram Bot** — Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- **Discord Webhook** — Set `DISCORD_WEBHOOK_URL`

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/VPC-byte/jobagent.git
cd jobagent
pip install -e .
```

### 2. Install Browser Runtime

```bash
playwright install chromium
```

### 3. Log in to Job Platforms

```bash
# Log in to LinkedIn (opens browser for manual login)
jobagent login --platform linkedin

# Log in to Boss直聘
jobagent login --platform boss

# Log in to all platforms at once
jobagent login --platform all

# Check if saved cookies are still valid
jobagent login --platform linkedin --check
```

### 4. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your settings (see [Configuration](#%EF%B8%8F-configuration) below).

### 5. Configure Agent Startup Context

```bash
cp profiles/search.example.yaml profiles/search.yaml
cp profiles/background.example.yaml profiles/background.yaml
cp profiles/agent.example.yaml profiles/agent.yaml
```

`search.yaml` contains desired roles, locations, and constraints. `background.yaml` contains
resume-derived experience, projects, and skills confirmed by you. They are separate concepts;
`agent.yaml` only references them. Raw resume parsing is deferred.

### 6. Launch!

```bash
# Start the single Agent and describe a company, role, and JD in natural language
jobagent chat --config profiles/agent.yaml
```

The Phase-1 Agent adaptively researches recent Xiaohongshu interview evidence, preserves post bodies
and images, runs OCR and evidence admission, and generates Markdown + JSON preparation packs. It is
read-only: it does not comment, send DMs/resumes, or submit applications automatically.

---

## 🔧 CLI Reference

### `jobagent login` — Platform Authentication

```bash
# Log in to LinkedIn
jobagent login --platform linkedin

# Log in to Boss直聘
jobagent login --platform boss

# Log in to all platforms
jobagent login --platform all

# Set login timeout (minutes)
jobagent login --platform linkedin --timeout 5

# Check cookie validity (no browser popup)
jobagent login --platform linkedin --check
```

### `jobagent scrape` — Scrape Only (No Applications)

```bash
# Scrape LinkedIn jobs
jobagent scrape --platform linkedin --query "AI Engineer" --limit 20

# Scrape Boss直聘
jobagent scrape --platform boss --query "后端工程师" --limit 20

# Scrape all platforms
jobagent scrape --platform all --query "Python Developer" --limit 30
```

> 💡 Use `scrape` first to check listing quality before running the full pipeline.

### `jobagent run` — Full Pipeline

```bash
# Basic usage
jobagent run --profile profiles/me.yaml --query "AI Engineer"

# Specific platform + limit
jobagent run --platform linkedin --profile profiles/me.yaml --query "ML Engineer" --limit 20

# All platforms
jobagent run --platform all --profile profiles/me.yaml --query "Backend Developer" --limit 50
```

### `jobagent validate-profile` — Validate Profile YAML

```bash
jobagent validate-profile --profile profiles/me.yaml
```

Catch YAML errors before running the pipeline.

---

## ⚙️ Configuration

### `.env` Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| **Core** | | | |
| `JOBAGENT_ENV` | No | `development` | Runtime environment |
| `JOBAGENT_LOG_LEVEL` | No | `INFO` | Log level |
| `JOBAGENT_HEADLESS` | No | `true` | Headless browser mode (set `false` to watch) |
| `JOBAGENT_MAX_JOBS` | No | `30` | Max jobs to process per run |
| `JOBAGENT_REQUEST_TIMEOUT` | No | `30` | Request timeout in seconds |
| **LLM** | | | |
| `JOBAGENT_LLM_PROVIDER` | No | `openai-compatible` | First release supports OpenAI-compatible only |
| `JOBAGENT_LLM_MODEL` | No | `gpt-4o-mini` | Model name or Volcengine Ark endpoint/model ID |
| `OPENAI_BASE_URL` | No | `https://api.openai.com/v1` | OpenAI-compatible base URL for OpenAI, DeepSeek, Volcengine Ark, etc. |
| `OPENAI_API_KEY` | For OpenAI-compatible | - | API key for the configured compatible provider |
| **Platform Cookies** | | | |
| `BOSS_COOKIE` | No | - | Boss直聘 cookie (overrides saved file) |
| `LINKEDIN_COOKIE` | No | - | LinkedIn cookie |
| **Boss直聘 Settings** | | | |
| `BOSS_GREETING` | No | - | Greeting template (`$company` `$title` `$name`) |
| `BOSS_APPLY_DELAY_MIN` | No | `3.0` | Min seconds between applications |
| `BOSS_APPLY_DELAY_MAX` | No | `8.0` | Max seconds between applications |
| `BOSS_DAILY_LIMIT` | No | `100` | Daily application cap (1-150) |
| `BOSS_SKIP_INACTIVE_DAYS` | No | `7` | Skip jobs where HR inactive for N days |
| **Notifications** | | | |
| `TELEGRAM_BOT_TOKEN` | No | - | Telegram Bot token |
| `TELEGRAM_CHAT_ID` | No | - | Telegram Chat ID |
| `DISCORD_WEBHOOK_URL` | No | - | Discord Webhook URL |
| **Network** | | | |
| `HTTP_PROXY` | No | - | HTTP proxy |
| `HTTPS_PROXY` | No | - | HTTPS proxy |

> Claude OAuth/Anthropic is not part of the first-release model path and will return later after
> Agent tool-call support is implemented and tested.

### `profiles/me.yaml` — Job Seeker Profile

```yaml
name: "Jane Doe"
email: "jane@example.com"
years_experience: 3

summary: >
  Full-stack developer with experience in AI/ML applications,
  cloud infrastructure, and DevOps. Looking for AI Agent roles.

skills:
  - Python
  - TypeScript
  - LLM/Agent Development
  - Kubernetes
  - Docker
  - AWS/GCP

desired_roles:
  - AI Agent Developer
  - LLM Application Engineer
  - MLOps Engineer
  - Backend Developer (AI)

preferences:
  locations:
    - "San Francisco"
    - "remote"
  salary_min: 120000   # Annual, USD
  salary_max: 200000
  work_schedule: "flexible"
  industries:
    - AI
    - Web3
    - Cloud
  deal_breakers:
    - "no remote option"
    - "contract only"
```

---

## 🏗️ Architecture

```text
+---------------------+
|   Profile Loader    |  Your resume / preferences (YAML)
+----------+----------+
           |
           v
+---------------------+      +----------------------+
|   Scraper Layer     |----->| Unified Job Objects  |
| LinkedIn / Boss直聘 |      | (Pydantic Models)    |
+----------+----------+      +----------+-----------+
           |                            |
           v                            v
+---------------------+      +----------------------+
|   LLM Matcher       |----->| Match Score + Reason |
| Claude OAuth/API/   |      | (SSE streaming)      |
| OpenAI              |      +----------+-----------+
+----------+----------+                 |
           |                            v
+---------------------+      +----------------------+
|  Auto Applier       |----->| Application Status   |
| LinkedIn / Boss直聘 |      | Applied / Failed /   |
+----------+----------+      | Skipped              |
           |                 +----------+-----------+
           +------------+---------------+
                        v
              +--------------------+
              | Notification Layer |
              | Telegram / Discord |
              +--------------------+
```

## 📂 Project Structure

```text
jobagent/
  applier/                 # Auto-application engines
    base.py                #   Applier base class
    boss.py                #   Boss直聘 (greeting + anti-detection)
    linkedin.py            #   LinkedIn Easy Apply
    captcha.py             #   CAPTCHA detection + notification
    history.py             #   Application history (dedup)
  auth/                    # Authentication
    browser_login.py       #   Playwright interactive browser login
    cookie_manager.py      #   Cookie persistence manager
    claude_auth.py         #   Claude OAuth credential reader
    token_refresh.py       #   OAuth token auto-refresh
  matcher/                 # LLM matching engine
    llm_matcher.py         #   Multi-provider match scoring
  models/                  # Claude API client
    claude_api.py          #   Claude API wrapper
    streaming.py           #   SSE streaming + retry/backoff
  notifier/                # Notifications
    telegram.py            #   Telegram Bot notifications
    discord.py             #   Discord Webhook notifications
  profile/                 # Profile loading
    loader.py              #   YAML profile parser
  scraper/                 # Job scrapers
    base.py                #   Scraper base class
    boss.py                #   Boss直聘 scraper
    linkedin.py            #   LinkedIn scraper
  cli.py                   # CLI entry point (Click)
  config.py                # Configuration (pydantic-settings)
  domain.py                # Data models (Pydantic)
profiles/
  example.yaml             # Profile template
docs/
  architecture.md          # Architecture docs
tests/
  test_models.py           # Tests
```

## 📋 Platform Support

| Platform | Scraping | Applying | Notes |
| --- | --- | --- | --- |
| **LinkedIn** | ✅ | ✅ | Easy Apply automation |
| **Boss直聘** (zhipin.com) | ✅ | ✅ | Playwright-simulated greeting |
| Lagou (拉勾) | 🔜 | 🔜 | Adapter in development |
| 51Job (前程无忧) | 🔜 | 🔜 | Adapter in development |

---

## 🤝 Contributing

PRs welcome! Especially:

- 🔌 **New platform adapters** (Indeed, Glassdoor, Lagou…)
- 🧠 **Better matching strategies** (prompt tuning, multi-dimensional scoring)
- 🔒 **Reliability improvements** (anti-detection, checkpoint/resume)
- 📢 **New notification channels** (Slack, WeChat, email)

```bash
pip install -e .[dev]
pytest -q
```

Please run tests, add type hints, and update docs before submitting.

## ⚠️ Disclaimer

Automated interaction with job platforms may be subject to their terms of service and local regulations. Use this tool responsibly, within your authorized scope. The authors are not liable for misuse.

## 📜 License

MIT License — see [LICENSE](./LICENSE).
