# Boss 卡片消息接入设计（type 9 简历请求卡片）

背景：2026-09-23 15:52，TCL HR（康先生）发出简历请求卡片（mid 389163931374337），
两条消息管道均未解析——`read_boss_conversation` 报 `text=""`，daemon 入站管道
直接丢弃。本文档固化补齐方案。

## 范围

**做**：

1. 读链路（Agent 视角）：`read_boss_conversation` 对无文本卡片消息给出
   语义标签 + 原始 `body_json` 片段（≤400 字符，供后续真机结构挖掘）。
2. 守护链路（daemon 视角）：轮询补录发现 HR 侧 type=9 消息时，幂等写入
   `resume_requests` 队列，与既有 WS 监听路径（`BossResumeRequestListener`）
   共用一张表。

**不做**：

- 不实现卡片「同意」按钮的自动点击。主动投递走 `/wapi/zpchat/exchange/test`
  （mitm 2026-09-20 实锤：只需 securityId + type=3，不需要卡片 mid），
  `boss_resume_delivery.py` 已实现完整闭环。
- 不解析 type=9 卡片 body 内部结构——raw 抓包从未拿到过（roadmap「中间档无
  请求/响应体」），只透传 `body_json` 片段。
- 不把图片/语音/表情等噪音类型灌入 daemon 入站管道（防 reply 分类器噪声）。

## 证据锚点（file:line，2026-09-23 快照）

| 事实 | 位置 |
|---|---|
| 消息类型枚举 TEXT=1…RESUME=9…（chat-core 实锤） | `docs/next-features-roadmap.md:150-155` |
| 读链路只抽文本，卡片 text 恒空 | `jobagent/applier/boss_chat.py` `_FETCH_CONVERSATION_JS` messages.map |
| daemon 丢弃无文本入站 | `jobagent/boss_daemon.py` `_inbound_from_raw`（`raise ValueError("inbound message has no text")`） |
| WS 侧 type-9 识别已存在（body_type==9） | `jobagent/applier/boss_ws.py` `find_resume_requests` |
| 幂等队列已存在，无人从轮询侧灌数据 | `jobagent/journey/resume_requests.py` `ResumeRequestQueue.receive`（UNIQUE(conversation_id, source_mid)） |
| 分类器已有 `resume_request` intent（fact 需求为空） | `jobagent/hr_reply/classifier.py:33,49` |
| archive 已存 msg_type + raw_json | `jobagent/journey/chat_archive.py` `boss_chat_messages` 表 |

## 架构

```text
historyMsg (HTTP, 轮询)                          WS/MQTT (实时, 既有)
  │ _FETCH_CONVERSATION_JS                         │ find_resume_requests (body_type==9)
  │  + bodyJson(≤400, 仅 text 为空时)              │
  ▼                                                ▼
read_conversation 后处理                     BossResumeRequestListener
  ├ type∈{9}(可行动): text="[简历请求卡片]"        │
  ├ 其他枚举类型: card_label 字段, text 保持空      │
  └ body_json 保留                              │
  ▼                                              ▼
Agent 工具返回(全类型可见)              resume_requests 表 (共用, mid 幂等)
  │
  ▼ daemon 轮询同源数据
boss_inbound_messages (type 9 因 text 非空自然入站)
  │
  ▼ _poll_owned: inserted 中 type==9 → ResumeRequestQueue.receive
```


跨路径幂等契约：`resume_requests` 以 UNIQUE(conversation_id, source_mid)
去重，两条入队路径（本设计的 daemon 轮询、既有 WS 监听）必须传同一格式
的 conversation_id——即 daemon 的 `str(friend.conversationId or friendId)`。
WS 监听接线时必须遵守（已钉在两个接缝的注释里）。

单一事实源：`RESUME_CARD_TYPE = 9` 定义在 `jobagent/journey/resume_requests.py`
（领域模块，无浏览器依赖），`boss_chat`/`boss_daemon` 引用它。

### 类型标签表（chat-core 枚举）

| type | 枚举 | 标签 | 处置 |
|---|---|---|---|
| 2/3/13 | SOUND/IMAGE/VIDEO | `[语音消息]`/`[图片消息]`/`[视频消息]` | 仅工具可见（card_label） |
| 4/7 | ACTION/DIALOG | `[动作卡片]`/`[对话卡片]` | 仅工具可见 |
| 8 | JOB_DESC | `[职位卡片]` | 仅工具可见 |
| **9** | **RESUME** | **`[简历请求卡片]`** | **写入 text，进 daemon 入站 + 队列** |
| 12 | HYPERLINK | `[链接消息]` | 仅工具可见 |
| 14 | INTERVIEW | `[面试卡片]` | 仅工具可见（开放问题①） |
| 19/20/25 | RESUME_SHARE/STICKER/STAR_RATE | `[简历分享]`/`[表情]`/`[评价]` | 仅工具可见 |

两档设计的原因：`text` 是 daemon 入站、reply 分类器、指纹去重的唯一通道——
只有系统有明确下游处理的类型（9 → `resume_request` intent + 投递闭环）才写
text；表情/图片若灌入会产生 awaiting_human 噪声。其余类型走独立 `card_label`
字段，LLM 工具输出里同样可见，但不触发行为。

### `_inbound_from_raw` 不改

`read_conversation` 已把 type 9 的 text 填为标签，daemon 归一化天然放行；
其余空文本卡片继续 `raise ValueError` 丢弃——行为边界保持显式。

### daemon 队列接线

`BossConversationDaemon.__init__(..., resume_queue: ResumeRequestQueue | None = None)`：

- `None` → 完全跳过（既有测试/调用零改动，兼容开关即参数本身）；
- `_poll_owned` 在 `store.record()` 返回的 **inserted**（去重后新入库）里筛
  `raw.type == RESUME_CARD_TYPE`，逐条 `queue.receive(source_mid=platform_message_id)`；
  mid 非数字 → warning 跳过（队列以 mid 为幂等键，无 mid 不能落）；
- `run()` finally 与 `close()` 关闭队列连接（sqlite close 幂等，与 store 同
  收尾模式）；
- baseline（冷启动）消息同样落卡：卡片不论新旧都可行动，mid 幂等防止与
  WS 侧重复。

## 数据模型

无新表、无迁移。复用 `resume_requests`（已存在）；`boss_chat_messages.raw_json`
随工具双写自然带上 `body_json`/`card_label`。

`card_payload` 存 read_conversation 映射后的消息 dict（含 type/text 标签/
body_json/time），与 WS 侧存的 protobuf 解码 dict 结构不同但字段兼容
（都含 mid/type），消费方只依赖 `conversation_id + source_mid`。

## 测试计划（用户可见场景）

1. Agent 读历史含 type 9 卡片 → 工具输出该消息 text=`[简历请求卡片]`，
   且带 body_json（TCL 15:52 场景的直接回归）。
2. 同一会话含 type 8 职位卡片 + type 1 文本 → 职位卡片不进 text 通道，
   文本消息零变化。
3. daemon 轮询到 HR type 9 消息 → `boss_inbound_messages` 有该行（text 为
   标签）且 `resume_requests` 恰好一行；重放同一 mid 不产生第二行。
4. daemon 不接 resume_queue（默认 None）→ 行为与现状完全一致。
5. 既有套件零改动通过。

## 诚实边界

- 标签 ≠ 内容：type 9 的 body 内部字段（职位名/卡片文案）仍未知，
  `body_json` 只是 ≤400 字符的截断透传；完整结构需一次 raw bytes 抓包。
- historyMsg 的 `type` 字段与 WS protobuf `body.type` 同值（chat-core 单一
  枚举）是强推断而非抓包实锤；若真机不符，type 9 会退化为「未识别卡片」
  工具可见、不进队列——不会错误行动。
- 冷启动会把历史上所有 type 9 卡片灌成 pending 行（无过期时间）；预期量级
  为个位数到几十，用户可逐条 reject。

## 开放问题

1. INTERVIEW=14 是否也应进 text 通道（分类器有 `interview_time` intent，
   恒人工）？待真机捕获 type 14 消息后再定。
2. `boss_ws.find_resume_requests` 仍硬编码 `body_type == 9`，可改引
   `RESUME_CARD_TYPE`（纯清理，行为不变）。
3. `resume_requests` 的消费端（status 推进到 waiting_approval → approved →
   confirmed 的自动化）尚未接线；当前止步于「received 落库 + Agent 可查」。
