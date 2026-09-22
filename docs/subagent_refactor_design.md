# JobAgent 主 Agent + Boss Sub-Agent 重构设计

状态：已实施（2026-09-22，两轮：先 5 路拆分，后按评审意见收窄为 4 路）。实现见
`jobagent/agent.py`（prompts + `platform_subagents` 装配），契约由
`tests/test_subagent_task_serialization.py::test_default_agent_exposes_platform_tools_only_to_their_subagents`
固定。

## 当前状态
- P0（root 工具过滤）已完成；Boss 工具全部隔离在子 Agent 内
- Office AI 路由已修复
- 单块 `boss_recruiting` → 5 路拆分 → 收窄为 4 路（conversation + resume_delivery 合并）

## 架构：4 子 Agent（阶段化）

### 主 Agent 工具（仅保留非平台工具）
- 文件系统：read_file, write_file, edit_file, glob, grep, execute
- 技能：list_skills, read_skill, install_skill
- 任务委派：task（框架内置，`SingleSubagentTaskMiddleware` 强制单飞）
- 状态库：search_history, get_job_progress, list_job_records, confirm_greeting_delivered 等
  （与 boss_verification 镜像共享；均无平台凭证）

### Boss 子 Agent（tool → subagent 映射）
| Subagent | 工具 | HITL (interrupt_on) |
|---|---|---|
| boss_discovery | discover_boss_jobs | — |
| boss_greeting | boss_greet_jobs, list_boss_greetings | boss_greet_jobs |
| boss_engagement | read_boss_conversation, reply_boss_greeting, prepare/send_boss_resume_after_hr_reply, upload_boss_resume_pdf | reply_boss_greeting, send_boss_resume_after_hr_reply, upload_boss_resume_pdf |
| boss_verification | read_boss_conversation（只读镜像）, confirm_greeting_delivered, get_job_progress, list_job_records | — |

设计要点：
- **阶段合并（conversation + resume_delivery → boss_engagement）**：常见链路
  "投递 → unverified → 读历史核验"原先要跨 2 次委派（投递子 Agent 没有会话读取工具，
  只能报告后由主 Agent 再派 conversation）。合并后核验在同一个 task 内完成，且
  "HR 是否已回复"的前置条件可由子 Agent 自己读会话确认。5 个工具仍限在单一阶段内，
  全部写工具 HITL 门控，不回到 8 工具大杂烩。
- **`_BOSS_CHANNEL_FACTS` 单一事实源**：WS 回执丢失/unverified 语义、身份铁律与
  refused 处理、HITL 暂停三条硬事实以一个常量注入所有外向子 Agent prompt，消除三处
  重复叙述的漂移风险。
- `read_boss_conversation` 在 engagement 与 verification 间只读镜像（同
  `_XHS_SHARED_TOOL_NAMES` 模式）。
- 状态库三件套不在 `_BOSS_TOOL_NAMES` 内，verification 从 root 过滤前快照
  （`all_registered_tools`）取用。
- 4 个子 Agent 共享一个 `BossSessionPreflight` 实例（单份 TTL 凭证缓存），middleware
  包装各自实例化。
- prepare 不设 HITL（只读预检、返回选项交用户选择）；send/upload/reply/greet 全部 HITL。

### 委派规则（主 Agent system prompt `<platform_subagent_policy>`）
- 按阶段路由：discovery → greeting → engagement（HR 已回复阶段）→ verification
- task 描述必须自包含（Job ID / conversation 标识、公司岗位、Journey ID、必要参数）
- 一次只派一个 task；可能外发时必须等回执
- engagement 的 unverified 由其自持会话读取工具在同一 task 内核验；greeting 的
  unverified 委派 verification 读历史核验；绝不让发送方子代理自行重发
- 严禁 `execute` + `write_file` 绕过子 Agent，严禁调用未注册的渠道原始 Tool

### 不变量（测试固定）
- 每个 Boss 子 Agent 的 prompt 反引号引用的工具名 ⊆ 其实际工具集（防 prompt/tool 漂移）
- 所有平台写工具在其所属子 Agent 内 HITL 门控；只读子 Agent 无 HITL 项
- 外向子 Agent prompt 均含冻结的渠道事实块（`渠道事实（实测 2026-09-19）`）
- 本地 PDF 简历库不进入任何 Boss 子 Agent

## 渠道事实（实测 2026-09-19，`_BOSS_CHANNEL_FACTS` 单一来源）
- WS/MQTT 回执经常丢失：unverified ≠ 失败，消息大概率已送达、约 6 秒后历史可见
- 绝不因 unverified 直接重发（重复消息风险）
- 出站护栏 refused → 以用户本人第一人称重写，不原样重试；绝不暴露 AI 身份
- 写工具自动暂停等待批准，绝不绕过

阶段专属事实（不在共享块）：批量 greet 后自动历史二次核验
（history_confirmed_after_batch）只在 greeting；预检凭证 10 分钟过期由 send 自动重新
预检只在 engagement。

## Review 记录
### 第一轮（5 路拆分实施时发现并修复）
1. P0 `<platform_subagent_policy>` 仍指向已删除的 `boss_recruiting` → 重写路由块
2. P0 `boss_conversation` 缺 `reply_boss_greeting` 的 interrupt_on（HITL 回归）→ 补
3. P0 `boss_verification` 工具集从 `_BOSS_TOOL_NAMES` 过滤取名 → 空工具子代理 → 改从
   `all_registered_tools` 快照取 + 镜像 `read_boss_conversation`
4. P1 prompts 引用子 Agent 未持有的工具 → 改为委派措辞
5. P1 渠道事实在拆分中被丢弃 → 逐条移植
6. P2 interrupt_on 过滤集合混入非 HITL 工具名 → 收敛为 `_boss_interrupts`
7. P0 `available_tools=None` 破坏 `_tools` 完整清单契约 → 恢复快照传参
8. P1 root prompt 指示"先用只读会话历史核验"但 root 无会话读取工具 → 改为委派
   boss_verification；仅用户明确确认送达时 root 才直接补登记

### 第二轮（改进建议处置）
- **采纳 #2**：提取 `_BOSS_CHANNEL_FACTS` 共享常量注入全部外向 prompt。
- **采纳 #3**：合并 conversation + resume_delivery 为 boss_engagement（理由见上）。
- **拒绝 #1**（撤销 boss_verification、收窄为 3 个）：状态穷举——root 持有
  `confirm_greeting_delivered` 但不持有 `read_boss_conversation`（`_BOSS_TOOL_NAMES`
  过滤），任何"先核验再补登记"的闭环在 root 都要跨委派才能拿到历史证据；boss_
  verification 是唯一能把"读历史 + 幂等补登记"压缩进单次委派的单元。撤销它会使
  greeting-unverified 场景退化为两跳，且 root 直调 confirm 无核验证据支撑（prompt
  已明令不得臆测送达）。拒绝理由同时写入 `JOB_PROGRESS_POLICY` 与本文档。

## 后续改进方向（未实施）
- 若实际使用中发现 engagement 的 5 工具面过宽（例如回复与投递混任务），可再拆回；
  反向操作已有完整 git 历史可参照。
