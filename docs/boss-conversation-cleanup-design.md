# Boss 会话清理设计（删除长期无回复 HR 会话）

背景：用户需要把长期不回复的 HR 会话从 Boss 聊天列表清掉。2026-09-24 12:53:59
经 mitmdump 全量记录器（`scripts/mitm_capture_wapi.py` → `.ua/wapi_capture.jsonl`）
抓获删除端点实锤。本文档固化工具设计。

## 端点契约（实测）

```text
POST https://www.zhipin.com/wapi/zprelation/friend/delete.json
Content-Type: application/x-www-form-urlencoded
body: securityId=<会话 securityId>          # 唯一参数，~~ 结尾
headers: zp_token(=cookie bst) + traceId + X-Requested-With   # historyMsg 同族
← {"code":0,"message":"Success","zpData":true}
```

`securityId` 即 `getGeekFriendList` 返回的会话凭据——`boss_chat.py` creds 步骤、
`boss_resume_delivery.py:52-63` 已在用，无新凭据链。

## 范围

**做**：

1. `select_boss_stale_conversations`（读工具，无 HITL）：本地数据筛出候选名单。
2. `delete_boss_conversations`（写工具，**HITL 必过**）：按确认名单页内 fetch
   逐会话删除 + 复核 + 审计落库。

**不做**：

- 不做"自动选择并直接删除"——名单必须经人工确认（物理 interrupt）。
- 不做撤销——平台无已知恢复 API（诚实边界）。
- 不做 CLI 入口（v1 只走 Agent 工具面）。
- 不猜 HR 侧效果（是否同步删除、能否重新发起）。

## 候选判据（"很久不回复"可操作化）

数据源：`boss_chat_messages.latest_per_friend()`（`chat_archive.py:143-167`，
每会话最后一行：who spoke last + when）。单位注意：`sent_at` 存的是
historyMsg 的 `time`（**秒**），与列表 `updateTime`（毫秒）不同，比较前统一
归一到毫秒。

入选（全部满足）：

- `last_direction == "geek"`——我方最后发言，HR 未回（含打招呼后从未获回复）；
- `last_sent_at` 距今 > `stale_days`（默认 14，可调 7-90）。

排除（保护过滤，任一命中即出局）：

- `resume_requests` 存在 pending 态行（received/waiting_approval/approved/
  accept_action_sent/resume_delivery_pending）的会话——简历闭环未完，删了丢上下文；
- `boss_reply_queue` 存在未决草稿的 friend_id；
- 最近 `grace_days`（默认 3）内 `boss_inbound_messages` 有该会话新消息；
- `protect_hr_names` 显式保护名单；
- 单批上限 `max_batch`（默认 10）。

`last_direction == "boss"`（HR 最后发言等我方回）永不入选——那是回复管线的
事，不是清理的事。

## 执行流（delete 工具，单次页内 evaluate）

复用停靠聊天页 + 单 evaluate 模式（warlock 按**页面加载轮次**计数，页内连续
fetch 不重载页面即安全——TR-6a.1 实测：列表刷新=纯 fetch）：

```text
parked chat page, ONE evaluate:
  1. geekFilterByLabel?labelId=0            → friendId → name/company/encryptBossId
  2. getGeekFriendList (friendIds= 批量)     → securityId 映射；缺凭据的会话记 skip
  3. for each target: POST friend/delete.json → 记 code；间隔 3-5s 随机节律
  4. geekFilterByLabel 再拉一次              → 复核目标已从列表消失
Python 侧:
  5. 审计行写入 boss_conversation_deletions；结果结构化返回
```

- evaluate 超时按 `max_batch × 节律上限 + 余量` 设（默认 ~120s）。
- **试水批次**：首次运行 `pilot=True` 限 3 个（单 evaluate 长跑的 warlock 反应
  未实测），成功后再放量。
- 单会话 code!=0 → 记录并继续（partial_success）；列表/凭据步骤失败 →
  typed failure（step: list/creds 模式），熔断器计入 page_lost/page_blocked。

## 二阶效应：greet 去重链（review 抓出的关键项）

`boss_greet` 的 `already_greeted_in_history` 去重依赖聊天列表/历史
（`boss_greet.py:409` `_load_history_job_index`）。**删除会话 = 抹掉平台的去重
证据** → 同一 HR/岗位可能被重复打招呼。

对策：本地 `boss_conversation_deletions` 表纳入 greet 去重链——greet 前除查
chat history 外，同 friend_id/encryptJobId 命中删除表即视为已接触（skip）。
本地事实源原则（roadmap 既有原则）的自然延伸：平台会话可删，接触史不可删。

## 数据模型

```sql
CREATE TABLE IF NOT EXISTS boss_conversation_deletions (
    id INTEGER PRIMARY KEY,
    friend_id INTEGER NOT NULL,
    hr_name TEXT NOT NULL,
    company TEXT NOT NULL,
    security_id_prefix TEXT NOT NULL,   -- 前 16 字符，审计可核对面不全量存凭据
    api_code INTEGER NOT NULL,
    verified_gone INTEGER NOT NULL DEFAULT 0,  -- 复核拉列表时已消失
    deleted_at INTEGER NOT NULL,
    UNIQUE(friend_id, deleted_at)
);
```

本地 `boss_chat_messages` 档案**保留不动**（候选来自档案，至少每会话一行；
未深读过的会话档案可能不全——接受）。

## HITL 展示（人工核对的最小信息集）

每行：HR 名 / 公司 / friend_id / 最后发言方向+时间 / 最后消息摘要 60 字 /
被排除原因（select 输出带 reason）。同名 HR 多会话靠 friend_id+company 消歧。

## 测试计划（用户可见场景）

1. select：geek 最后发言 20 天前 → 候选；boss 最后发言 → 排除；pending
   resume_request → 排除；3 天内有 inbound → 排除；秒/毫秒混存不出错。
2. delete（fake evaluate）：多会话逐个调用、节律存在、审计行落库、复核
   步骤执行、单会话失败不阻断后续（partial_success）。
3. HITL 注册：`delete_boss_conversations` ∈ `_HITL_TOOLS`（仿
   `test_boss_resume_upload.py:24-27`）。
4. greet 去重：删除表命中 → already_contacted/skip。
5. 既有套件零改动通过（select 为新读工具，delete 默认不进任何自动链路）。

## 里程碑（可发版切口）

- M1：select 工具（纯本地读）——单独可用，立即产生价值（看清谁在僵着）。
- M2：delete 工具 + 审计 + greet 去重接线（试水批次跑通后放量）。

## 诚实边界

- 平台侧删除不可逆、无撤销 API；`verified_gone` 只证明列表里消失。
- HR 侧是否同步删除、能否重新发起会话——未知；若 HR 重新打招呼会建新会话
  （新 securityId），属正常新流程。
- 单 evaluate 内 3-10 连删的 warlock 反应未实测——pilot 批次就是为此设的。
- `confirmed` 态简历投递 >stale_days 无回复视为正常 stale 可删：投递事实在
  本地 `resume_deliveries` 表，不依赖会话存在。
