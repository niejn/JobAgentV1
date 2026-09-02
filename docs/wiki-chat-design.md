# XHS Wiki Chat — 设计文档

> 完全独立于 JobAgent 求职管线。基于小红书数据源的生活知识 RAG 问答系统。
> 支持快速问答（~10-20s）和深度调研（~5-30 分钟，多轮 + 评论 + OCR + 断点续跑）。

---

## 目录

1. [定位与原则](#1-定位与原则)
2. [架构总览](#2-架构总览)
3. [多节点图设计](#3-多节点图设计)
4. [工具集与 HITL 矩阵](#4-工具集与-hitl-矩阵)
5. [存储设计](#5-存储设计)
6. [限流与风控策略](#6-限流与风控策略)
7. [OCR 策略](#7-ocr-策略)
8. [深度调研循环](#8-深度调研循环)
9. [断点续跑](#9-断点续跑)
10. [多会话并发（单例模式）](#10-多会话并发单例模式)
11. [CLI 接口](#11-cli-接口)
12. [文件结构](#12-文件结构)
13. [实施阶段](#13-实施阶段)
14. [决策记录](#14-决策记录)

---

## 1. 定位与原则

### 定位

> 一个以小红书为数据源的生活知识 RAG 问答系统，**与找工作无关**。

### 核心原则

- **数据源隔离**：只读 XHS，不写任何平台（不投递、不评论）
- **配置隔离**：独立 LLM 模型/参数配置，独立限流器，独立工具集
- **存储隔离**：独立 SQLite 数据库，独立 LangGraph checkpointer
- **防风控**：慢速串行爬取，随机抖动，独立限流，缓存优先
- **灾备优先**：每步落盘 checkpoint，可从中断处续跑

### 两种模式

| | 快速问答 `--quick` | 深度调研（默认） |
|---|---|---|
| 耗时 | ~10-20s | ~5-30 分钟 |
| 数据量 | 5-8 篇帖子正文 | 15-40 篇 + 每篇评论 + OCR |
| 流程 | 单轮：重写→搜索→抓正文→回答 | 多轮循环：搜索→抓取+OCR→评估→继续/报告 |
| 中断恢复 | 不需要（一轮即完成） | 需要 checkpoint 续跑 |
| 典型问题 | "上海五角场哪里停车便宜" | "深度调研上海各家医院胃病就诊费用对比" |

---

## 2. 架构总览

```
                        ┌───────────────────────────────────────────────┐
                        │            Supervisor Node                    │
                        │  (LLM, 独立 wiki LLM 配置)                     │
                        │  职责：理解用户意图 → 调度子节点 → 质量评估 → 汇总 │
                        └───────┬───────────────┬──────────────┬─────────┘
                                │               │              │
                   ┌────────────▼──────┐  ┌─────▼──────┐  ┌───▼──────────┐
                   │    Crawler Node   │  │   OCR Node │  │ Report Node  │
                   │                   │  │            │  │              │
                   │ 串行（防风控）    │  │ 并行（本地)│  │ 流式输出     │
                   │ 搜索 XHS          │  │ 下载图片   │  │ 生成 Markdown│
                   │ 抓取正文+评论     │  │ Tesseract  │  │              │
                   │ 下载图片          │  │ OCR识别    │  │              │
                   └────────┬──────────┘  └──────┬──────┘  └──────────────┘
                            │                    │
                            └────────┬───────────┘
                                     │
                            ┌────────▼────────┐
                            │   WikiStore     │
                            │  SQLite 缓存    │
                            │  + 会话持久化   │
                            │  + 去重逻辑     │
                            └──────┬──────────┘
                                   │
                            ┌──────▼──────────┐
                            │ SpiderXhsBackend │
                            │ (已有 + 新增     │
                            │  fetch_comments) │
                            └──────────────────┘
```

### 数据流

```
用户输入 "上海看胃病哪家医院好"
  │
  ▼
Supervisor: 重写 query → ["上海 胃病 医院 推荐", "上海 胃镜 医院 经验", "上海 消化科 哪家好"]
  │
  ▼
Crawler: 串行搜 3 个 query → 去重 → 每篇 fetch_note + fetch_comments + download_images
  │  (每步间隔 4-6s，写入 WikiStore)
  ▼
Supervisor: 检查质量 → 是否覆盖"医院对比、费用、挂号难度、体验"？
  ├─ 不够 → 反馈 Crawler ("缺胃镜费用信息") → 继续搜索
  │
  └─ 够 →
       │
       ▼
       OCR Node: 并行 Tesseract 处理所有图片（纯本地，无风控风险）
       │
       ▼
       Evaluator: LLM 评估覆盖度，确认足够
       │
       ▼
       Report Node: 生成分节 Markdown 报告，流式输出
```

---

## 3. 多节点图设计

### 图结构

```
                    ┌──────────────────┐
                    │   supervisor     │  ← 决策节点（LLM）
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
         crawler          ocr          report_generator
         (搜索+抓取)      (图片OCR)     (生成报告)
              │              │
              └──────────────┘
                     │
                     ▼
                evaluator
              (LLM 评估覆盖)
                     │
              ┌──────┴──────┐
              │             │
              ▼             ▼
           supervisor    report_generator
           (继续搜索)     (足够，输出)
```

### 节点职责

| 节点 | 工具 | 输入 | 输出 | 行为特征 |
|---|---|---|---|---|
| **supervisor** | 无（LLM 决策） | 全部 state | 下个节点名 + 参数 | 分析覆盖度，决定下一步 |
| **crawler** | `search_xhs`, `fetch_note`, `fetch_comments`, `download_images` | 搜索 query | 新 notes + comments | **串行**，防风控 |
| **ocr** | `run_ocr` | note_id 列表 | OCR 文本 | **并行**（纯本地） |
| **evaluator** | 无（LLM 评估） | 当前全部数据 | 覆盖度评估 + 缺口反馈 | 纯 LLM 调用 |
| **report_generator** | 无（LLM 生成） | 全部数据 | 分节 Markdown 报告 | 流式输出 |

### State 定义

```python
@dataclass
class WikiChatState:
    # 输入
    original_query: str
    rewritten_queries: list[str]           # LLM 重写后的搜索关键词
    max_notes: int                         # 最多抓取笔记数
    max_comments_per_note: int             # 每篇最多取评论数
    max_minutes: float                     # 深度调研最大时间（分钟）

    # 进度
    session_id: str
    status: Literal["in_progress", "completed", "failed"]
    step: int                              # 当前轮次
    error: str | None

    # 数据
    executed_queries: list[str]            # 已执行的搜索（防止重复）
    fetched_note_ids: list[str]            # 已抓取的 note_id
    notes: dict[str, NoteData]             # note_id → 帖子数据
    comments: dict[str, list[CommentData]] # note_id → 评论列表
    ocr_completed: list[str]               # 已完成 OCR 的 note_id
    ocr_texts: dict[str, str]              # note_id → OCR 识别文本

    # 评估
    coverage: QualityAssessment | None     # 覆盖度评估
    feedback: list[str]                    # 评估反馈（给 crawler 的缺口提示）

    # 输出
    report: str | None                     # 最终报告
    report_name: str | None                # 报告文件名（LLM 生成）
```

---

## 4. 工具集与 HITL 矩阵

### 工具定义

```python
@dataclass
class NoteData:
    note_id: str
    url: str
    title: str
    body: str
    author_name: str
    author_id: str
    tags: list[str]
    image_urls: list[str]
    published_at: str | None

@dataclass
class CommentData:
    comment_id: str
    note_id: str
    author_name: str
    content: str
    likes: int
    parent_id: str | None       # None=一级评论, 非空=回复
```

### HITL 矩阵

| 工具 | 网络请求 | HITL | 原因 |
|---|---|---|---|
| `search_and_fetch` | ✅ 搜索 (串行) | ❌ | 只读，无损 |
| `fetch_comments` | ✅ 1 次 API (串行) | ❌ | 只读 |
| `download_images` | ✅ 多张图片 (串行) | ✅ **HITL** | 写磁盘，消耗存储 |
| `run_ocr` | ❌ | ✅ **HITL** | CPU 消耗大 |
| `check_quality` | ❌ | ❌ | 纯 LLM 评估 |
| `generate_report` | ❌ | ❌ | 纯输出 |

**HITL 交互方式**：首次使用时汇总一次性询问：

```
⚠️ 将下载约 15 张图片到本地（预计 5MB），并启动 OCR 识别（约 30s）。
继续？[y/N]
```

不每篇问，不打断流程。

---

## 5. 存储设计

### 文件布局

```
data/wiki/
├── checkpoints.db          # LangGraph SQLite checkpointer
├── wiki_store.db           # 应用层缓存 + 会话
├── downloads/              # 图片下载
│   └── {note_id}/
│       ├── detail.txt
│       ├── image_0.jpg
│       ├── image_1.jpg
│       └── raw_response.json
└── reports/                # 深度报告
    └── {session_id}_{short_description}.md
```

### wiki_store.db 表结构

```sql
-- 搜索缓存（TTL: 1 小时）
CREATE TABLE wiki_cache (
    query_hash TEXT PRIMARY KEY,       -- sha256(query)
    query TEXT NOT NULL,
    results_json TEXT NOT NULL,        -- [{note_id, url, title, author_name}]
    fetched_at TEXT NOT NULL           -- ISO 8601
);

-- 帖子正文缓存（永久）
CREATE TABLE wiki_notes (
    note_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    author_name TEXT NOT NULL,
    author_id TEXT NOT NULL,
    tags_json TEXT NOT NULL,           -- JSON array
    image_urls_json TEXT NOT NULL,     -- JSON array
    published_at TEXT,
    fetched_at TEXT NOT NULL
);

-- 评论缓存（永久）
CREATE TABLE wiki_comments (
    note_id TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    likes INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (note_id, comment_id)
);

-- OCR 文本缓存（永久）
CREATE TABLE wiki_ocr_text (
    note_id TEXT PRIMARY KEY,
    ocr_text TEXT NOT NULL,
    image_count INTEGER NOT NULL,
    ocr_finished_at TEXT NOT NULL
);

-- 会话进度（持久化覆盖）
CREATE TABLE wiki_sessions (
    session_id TEXT PRIMARY KEY,
    original_query TEXT NOT NULL,
    depth TEXT NOT NULL CHECK(depth IN ('quick', 'deep')),
    status TEXT NOT NULL DEFAULT 'in_progress'
        CHECK(status IN ('in_progress', 'completed', 'failed')),
    report_json TEXT,                  -- 最终报告（completed 后写入）
    report_name TEXT,                  -- 报告文件名
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### 去重逻辑

```python
def should_skip_note(note_id: str) -> bool:
    """检查是否已抓取过（避免重复网络请求）。"""
    return note_id in wiki_notes

def dedup_results(results: list[NoteRef]) -> list[NoteRef]:
    """跨 query 结果去重，保留第一次出现的。"""
    seen: set[str] = set()
    deduped = []
    for ref in results:
        if ref.note_id not in seen:
            seen.add(ref.note_id)
            deduped.append(ref)
    return deduped
```

---

## 6. 限流与风控策略

### 独立限流器

不复用求职管线的 XHS 限流器，避免两类任务互相踩频率。

```python
# wiki 独立限流器（比求职更保守）
wiki_api_limiter = PyrateBlockingRateLimiter(
    requests=settings.xhs_wiki_api_rate_requests,      # 默认 10
    period_seconds=settings.xhs_wiki_api_rate_period,   # 默认 60
    name="xhs-wiki-api",
)
```

### 防风控策略

| 策略 | 实现 |
|---|---|
| **独立限流器** | 不与求职管线共享 |
| **串行请求** | 搜索、抓取、评论逐一进行，绝不多请求并行 |
| **随机抖动** | 每次请求后 sleep 0.5-2s（随机），不规律模式 |
| **缓存优先** | 已抓过的帖子 zero 网络请求 |
| **评论限量** | 默认 10 条高赞，不翻页（1 次 API 调用） |
| **速率自调节** | 高互动帖（>10 条评论的回复）主动降速 |
| **图片下载单线程** | 图片也串行下载，不并发 |

---

## 7. OCR 策略

### 默认开启

OCR 是深度调研的核心能力，因为大量生活知识以图片形式存在（菜单、价目表、对比图）。

### 执行流程

```
Crawler 完成（下载图片后）
  → Supervisor 调度 OCR Node
  → OCR Node 并行处理所有未 OCR 的图片
  → 结果写入 wiki_ocr_text 表
  → 通知 Supervisor 完成
```

### 并行策略

| 操作 | 并行度 | 说明 |
|---|---|---|
| 图片下载 | **串行** | 网络请求，防风控 |
| OCR 识别 | **并行**（asyncio.to_thread） | 纯本地 Tesseract，无风控风险 |
| 数据整理 | **并行** | 纯本地 |

### 引擎

复用现有 `TesseractOcrExtractor`（`jobagent_ocr_*` settings），与 interview 管线共享。

---

## 8. 深度调研循环

### 评估频次

**每轮搜索后评估一次**（一轮 = 3 个 query，约 15-20 篇新帖）。

### 循环流程

```
iteration 1:
  supervisor 重写 query → 3 个搜索关键词
  crawler 搜索(3) → 抓取(每query 前 N) → 评论(每篇 10 条) → 图片下载
  ocr 并行处理所有图片
  evaluator LLM 评估覆盖度
    ├─ sufficient → report_generator → 完成
    └─ insufficient → feedback → iteration 2

iteration 2:
  supervisor 基于 feedback 生成新搜索关键词
  crawler 搜索新 query（去重已抓帖子）
  ... 同上循环 ...

iteration N (达上限或时间到):
  evaluator 用现有数据生成"部分报告"
  report_generator 标注"部分调研，可能遗漏 X 方面"
```

### 覆盖度评估

```python
@dataclass
class QualityAssessment:
    sufficient: bool
    covered_aspects: list[str]          # 已覆盖的方面
    missing_aspects: list[str]          # 缺失的方面
    feedback: list[str]                 # 给 crawler 的缺口提示
    reasoning: str                      # 评估理由
```

### 中止条件

| 条件 | 动作 |
|---|---|
| 覆盖度足够 | 生成完整报告 |
| 已达最大轮次（默认 3） | 生成部分报告，标注"可能遗漏" |
| 已达时间上限（默认 25 分钟） | 保存 checkpoint，状态 in_progress，下次可续 |
| 用户 Ctrl+C | 捕获信号，保存 checkpoint，提示可续 |

---

## 9. 断点续跑

### checkpoint 机制

使用 LangGraph 的 SQLite checkpointer，与 `WikiChatState` 结合：

```python
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver.from_conn_string(
    "data/wiki/checkpoints.db"
)

graph = StateGraph(WikiChatState)
# ... 节点定义 ...
app = graph.compile(checkpointer=checkpointer)
```

**每个会话 = 一个 thread_id**。中断后根据 thread_id 加载 checkpoint，从上次中断处继续。

### 续跑流程

```
wiki-chat --resume
  1. 查 wiki_sessions 是否有 status=in_progress
    ├─ 无 → 提示无未完成的会话
    └─ 有一个 → 显示会话信息
         │
         ├─ 用户选择继续 → 加载 checkpoint → 从上次断点继续
         └─ 用户选择取消 → 无操作
```

### 中断处理

| 场景 | 行为 |
|---|---|
| **Ctrl+C** | 捕获 → 保存 checkpoint → status=in_progress → 提示"已保存，可 `wiki-chat --resume` 继续" |
| **进程崩溃** | 上次 checkpoint 完好（每步落盘）→ 重启后续跑 |
| **超时（25 分钟）** | 保存 checkpoint → status=in_progress → 下次可续 |

---

## 10. 多会话并发（单例模式）

### 规则

- **v1 只允许一个 in_progress 会话**（避免互相踩频率）
- 已有 in_progress 时：

```
wiki-chat "上海胃病医院"           → 提示"当前有未完成的深度调研「上海胃病医院对比」，是否继续？[y/N]"
                                     N → 询问是否终止旧会话，开新会话
wiki-chat "上海胃病医院" --force   → 自动终止旧会话 (status=terminated)，开新会话
wiki-chat --resume                  → 继续上次的 in_progress
```

### --force 行为

```
发现已有 in_progress 会话 "上海胃病医院对比"
--force 指定 → 终止旧会话 → 标记为 terminated → 开新会话
```

---

## 11. CLI 接口

```bash
# 快速问答（单轮，~10-20s）
jobagent wiki-chat "上海五角场哪里停车便宜" --quick

# 深度调研（默认，多轮，5-30 分钟）
jobagent wiki-chat "深度调研上海各家医院胃病就诊费用对比"

# 深度调研 + 参数调整
jobagent wiki-chat "上海家装公司排名" \
  --max-minutes 15 \
  --max-notes 30 \
  --max-comments 20

# 断点续跑
jobagent wiki-chat --resume

# 强制新会话（终止旧的）
jobagent wiki-chat "上海胃病医院" --force

# 查看报告
jobagent wiki-chat report <session_id>

# 列出所有历史会话
jobagent wiki-chat list
```

### 流式输出

```
# 状态事件
STATUS: 正在搜索：上海 胃病 医院 推荐 (1/3)
STATUS: 已抓取 5 篇，正在获取评论...
STATUS: OCR 识别中，预计 30s...

# 深度调研额外状态
STATUS: 第一轮完成，已收集 15 篇帖子
STATUS: 评估覆盖度：已覆盖「医院推荐、费用」，缺少「挂号难度」
STATUS: 第二轮搜索中...

# 报告输出（流式 token）
TOKEN: 根据小红书用户分享，上海多家医院提供胃病诊疗服务...
TOKEN: **中山医院**：普通胃镜约 300 元，无痛胃镜约 600 元...
TOKEN: **瑞金医院**：消化内科较强，挂号难度较大...

# 来源引用
SOURCE: 中山医院胃镜体验 - https://xiaohongshu.com/explore/xxx
SOURCE: 上海胃病医院排名 - https://xiaohongshu.com/explore/yyy
```

---

## 12. 文件结构

```
jobagent/wiki_chat/
├── __init__.py
├── cli.py                      # wiki-chat / wiki report / wiki list 命令
├── graph.py                    # LangGraph 图定义 + 编译
├── state.py                    # WikiChatState 定义
├── agents/
│   ├── __init__.py
│   ├── supervisor.py           # Supervisor 节点（LLM 决策路由）
│   ├── crawler.py              # Crawler 节点（搜索+抓取+评论+图片）
│   ├── ocr.py                  # OCR 节点（并行 Tesseract）
│   ├── evaluator.py            # Evaluator 节点（LLM 覆盖度评估）
│   └── report.py               # Report 节点（生成报告）
├── tools/
│   ├── __init__.py
│   ├── search.py               # search_and_fetch 工具
│   ├── comments.py             # fetch_comments 工具
│   ├── ocr.py                  # run_ocr 工具
│   ├── quality.py              # check_quality 工具
│   └── report.py               # generate_report 工具
├── store.py                    # WikiStore（SQLite 缓存 + 去重 + 会话）
├── retriever.py                # XhsWikiRetriever（搜索+抓取+评论）
├── prompts.py                  # 系统提示词
├── resume.py                   # 断点续跑逻辑
└── config.py                   # WikiSettings 配置

tests/
├── test_wiki_store.py
├── test_wiki_retriever.py
├── test_wiki_graph.py
├── test_wiki_crawler.py
├── test_wiki_ocr.py
└── test_wiki_cli.py
```

---

## 13. 实施阶段

| Phase | 内容 | 文件 | 新依赖 | 测试数（估） |
|---|---|---|---|---|
| **P1** | `SpiderXhsBackend.fetch_comments` + `XhsComment` 类型 | `xhs_backend.py` | 无 | 5 |
| **P2** | `WikiStore`（4 缓存表 + session 表 + 去重） | `store.py` | 无 | 10 |
| **P3** | `XhsWikiRetriever`（搜索+抓取+评论+图片下载，串行限流+抖动） | `retriever.py` | 无 | 8 |
| **P4** | Supervisor Agent 图 + 全部工具（带 HITL）+ 独立 LLM 配置 + `config.py` | `graph.py`, `agents/*`, `tools/*`, `state.py`, `config.py` | 无 | 15 |
| **P5** | OCR Node（并行 Tesseract）+ Evaluator Node + 质量评估反馈循环 | `agents/ocr.py`, `agents/evaluator.py`, `tools/ocr.py`, `tools/quality.py` | 无 | 10 |
| **P6** | `wiki-chat` CLI + quick + deep + 断点续跑 + 单例 + --force | `cli.py`, `resume.py`, `prompts.py` | 无 | 10 |
| **P7 (future)** | 多账户并发、语义缓存 Chroma | 增强 | `chromadb` | - |

---

## 14. 决策记录

### 已确认决策

| # | 决策 | 值 | 时间 |
|---|---|---|---|
| D1 | 评论限量 | 默认 10 条高赞，高互动帖（>100 评论）取 20 条，可配置 `--max-comments` | 2026-09 |
| D2 | 深度调研时间上限 | 默认 25 分钟，可 `--max-minutes` 调 | 2026-09 |
| D3 | 阻断恢复 | 交互确认（用户选择继续/取消） | 2026-09 |
| D4 | 报告命名 | `{session_id}_{short_description}.md`，short_description 由 LLM 生成 | 2026-09 |
| D5 | quick 模式 | 也写 checkpoint（一轮即 done，不中断） | 2026-09 |
| D6 | 多会话并发 | 单例模式，已有 in_progress 时提示，--force 杀旧开新 | 2026-09 |
| D7 | 查询重写 | 每次 chat 先 LLM 重写用户 query 为 1-3 个 XHS 搜索关键词 | 2026-09 |
| D8 | OCR | 默认开启，sub-agent 并行执行（纯本地 Tesseract） | 2026-09 |
| D9 | 搜索并行度 | 爬虫串行（防风控），OCR/整理数据并行 | 2026-09 |
| D10 | 搜索数量 | 可配置（`--max-notes`），跨 query 去重 | 2026-09 |
| D11 | 抓取深度 | fetch_note（文本）+ download_images（图片）都要 | 2026-09 |
| D12 | 会话历史 | 使用 LangGraph SQLite checkpointer | 2026-09 |
| D13 | LLM 配置 | 独立 wiki LLM 配置（`jobagent_wiki_llm_*`），fallback 到 `jobagent_llm_*`，工具集完全不同 | 2026-09 |
| D14 | HITL | `download_images` 和 `run_ocr` 需要 HITL，首次批量问一次 | 2026-09 |
| D15 | 图结构 | 多节点图（supervisor → crawler / ocr / evaluator / report_generator 独立节点） | 2026-09 |
| D16 | OCR 引擎 | 复用现有 Tesseract（`jobagent_ocr_*` settings） | 2026-09 |
| D17 | 评估频次 | 每轮搜索后评估（一轮 = 3 个 query，约 15-20 篇新帖） | 2026-09 |
| D18 | 图片下载 HITL | 首次批量问，不每篇问 | 2026-09 |

### 设计原则

```md
- 数据源隔离：只读 XHS，不写任何平台
- 配置隔离：独立 LLM 配置，独立限流器，独立工具集
- 存储隔离：独立 SQLite 数据库，独立 LangGraph checkpointer
- 防风控：慢速串行爬取，随机抖动，独立限流，缓存优先
- 灾备优先：每步落盘 checkpoint，可从中断处续跑
- 搜索去重：跨 query 去重，缓存命中免网络请求
- 评论高赞优先：取前 10 条高赞评论，1 次 API 调用
- OCR 并行：纯本地操作，可自由并发
- HITL 最小化：批量问一次，不打断流程
```
