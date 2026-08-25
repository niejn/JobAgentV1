# 下一阶段功能路线图（2026-08-25 讨论）

> 本文档收录近期讨论定稿的功能规划。原则：每个功能分小切片交付，高风险外部写操作一律
> HITL（人工确认），不追求一次做完整。状态标记：✅ 已交付 / 🚧 进行中 / 📋 待办。

五个功能按用户价值与风险排序：

| # | 功能 | 核心价值 | 风险 | 状态 |
|---|---|---|---|---|
| F1 | 岗位进度登记册 | 去重 + 查漏 + 长周期状态追踪 | 低（本地） | ✅ 核心已交付 |
| F2 | JD 定制打招呼语 | 提高沟通成功率 | 中（写操作已有 HITL） | ✅ 管道已交付 |
| F3 | 定制简历版本库 + Boss 简历同步 | 面试时知道自己投的是哪版简历 | TR-3/4 高（平台写） | 📋 |
| F4 | HR 消息流轻量 Gateway | HR 回复自动跟进，不依赖用户主动 chat | 中（平台读+写） | 📋 |
| F5 | 面试录音复盘 + 错题集 | 面试经验沉淀，避免重复踩坑 | 低（本地） | 📋 |

---

## F1. 岗位进度登记册 ✅ 核心已交付（2026-08-25）

### 已交付

- `jobagent/journey/job_registry.py`：SQLite 登记册，表 `job_records`（按稳定岗位身份
  `boss:<encryptJobId>` 去重）+ `job_status_events`（全量状态流转历史）。
- 状态机：`discovered -> recommended -> greeted -> hr_replied / no_response ->
  interviewing -> offer / rejected -> closed`；closed 可因 HR 迟来回复重开；
  非法流转抛 `JobTransitionError`，不静默回归。
- `discover_boss_jobs` 自动入册：新岗位记 `discovered`；已知岗位返回
  `progress_status`/`is_new`/`greeted_at`，已打招呼/面试中的岗位 Agent 不再重复推荐。
- `boss_greet_jobs` 发送成功自动标 `greeted`（确定性代码，不依赖 LLM 自觉）；
  registry 记录失败不阻断发送批次。
- 新 Agent Tools：`update_job_progress`（记录 HR 回复/约面/offer/被拒等）、
  `get_job_progress`（含完整事件历史，面试准备用）、`list_job_records`
  （按状态/公司筛选；`discovered` 未推荐岗位查漏）。
- System prompt 新增 `<job_progress_policy>`。
- 验证基线：`271 passed, 1 skipped`；Ruff、mypy 全绿。

### 后续切片 📋

| 切片 | 内容 | 备注 |
|---|---|---|
| F1-R1 | `/status` 板与 registry 合并视图：分析板（opportunity artifacts）+ 沟通板（registry）两栏展示 | 聊天内命令扩展 |
| F1-R2 | Application Eligibility Policy：同公司多岗位、冷却期、人工覆盖的结构化投递资格判定（plan.md §8.4 R4） | 投递前强制 |
| F1-R3 | 每周推荐长任务 + 周报（plan.md §8.4 R3） | 依赖 F4 消息数据更佳 |
| F1-R4 | Job Identity 升级：同岗位不同 encryptJobId / 跨平台识别（规范化公司+职位） | 第二阶段 |

---

## F2. JD 定制打招呼语 ✅ 管道已交付（2026-08-25）

### 已交付

- `BossApplier.apply(job, profile, greeting=...)`：个性化招呼语优先于 `$company/$title/$name`
  模板；都缺省时维持 Boss 默认招呼。
- `boss_greet_jobs` 的 `GreetingTarget.greeting` 字段：Agent 为每个岗位传入定制文本。
- System prompt 新增 `<greeting_policy>`：基于 JD 核心要求 + 已确认 Candidate Background
  生成差异化招呼语，禁止模板播报、禁止虚构经历，发送前必须连同岗位一起经用户确认（HITL）。

### 后续切片 📋

| 切片 | 内容 | 备注 |
|---|---|---|
| F2-G1 | 招呼语历史库：每条发送的招呼语文本 + 岗位 + 后续 HR 是否回复入册 | 依赖 F4 消息流；为 G2 提供数据 |
| F2-G2 | 打招呼 A/B 复盘：按招呼语风格/长度/切入点统计回复率，向用户提出改写建议 | 数据驱动 |

---

## F3. 岗位定制简历版本库 + Boss 站内简历同步 📋

**用户痛点**：每个岗位都改了简历（全集的子集），面试时必须查回投的是哪一版，避免
"简历包装了自己不知道"、回答牛头不对马嘴。

### 架构

```text
JD + Candidate Background
  -> Tailored Resume 生成（只能选材/压缩/重排已确认事实，禁止新增）
  -> 版本库（per-opportunity: resume-v001.md 递增，manifest 记录状态与来源映射）
  -> [HITL] 用户确认 -> 状态 confirmed
  -> Boss 投递成功 -> 状态 submitted + submitted_at
  -> 面试准备时：get_tailored_resume 查回该岗位投递版本 -> 准备包引用
```

### 切片

| 切片 | 内容 | 风险 | 验收 |
|---|---|---|---|
| TR-1 | 版本库：`LocalOpportunityArtifacts` 扩展 `save_tailored_resume` / `confirm` / `mark_submitted` / `read`；manifest 增加 `resumes[]`（version/sha256/status/时间戳/事实来源映射） | 零 | 同一 Opportunity 多版本递增、不可覆盖；状态只进不退 |
| TR-2 | Agent Tools：`save_tailored_resume`（存 draft）、`confirm_tailored_resume`（用户确认）、`get_tailored_resumes`（面试查回）；prompt 政策：每事实映射 Candidate Background，投递前必须 confirmed | 零 | 无 confirmed 简历的岗位不允许走投递流程 |
| TR-3 | `update_boss_online_resume`：CDP 打开 Boss 简历编辑页，自动更新"个人优势"等简单模块，复杂模块（工作经历富文本）导航到位并提示用户手动完成；HITL 确认参数 + 结果确认 | 高 | 真机校准选择器；失败如实报告，不伪造成功 |
| TR-4 | `upload_boss_resume_pdf`：CDP filechooser 上传附件简历 PDF；PDF 第一版由用户提供，markdown->PDF 转换后置 | 高 | 上传后页面出现新附件；风控/失败路径有测试 |
| TR-5 | `boss_greet_jobs` 成功后自动 `mark_submitted`，招呼关联简历版本 | 低 | 投递记录含 resume version |

### 风险与原则

- Boss 简历编辑页是复杂 SPA，选择器必须真机校准，先自动化最简单、对第一印象影响最大的
  模块，不追求全自动。
- 站内简历更新和 PDF 上传都是平台写操作：默认 HITL，展示将执行的具体内容，用户 approve
  才执行；失败路径全部结构化返回。
- 本地版本库是唯一事实源；Boss 站内状态只是投递渠道的镜像。

---

## F4. HR 消息流轻量 Gateway 📋

**用户痛点**：HR 的回复不该等用户打开 chat 才处理；Agent 应持续跟进沟通流。

### 设计定位：轻量，不做 OpenClaw/Hermes

不引入常驻消息总线、WebSocket 服务端或独立网关进程。复用既有模式：CDP 连接用户真实
Chrome + 被动监听页面自身 API 响应（与 `boss_cdp` 抓岗位列表同一套验证过的手法）。

```text
jobagent watch（后台 asyncio 进程，可单独启动）
  -> 定时（默认 10-15 分钟 + 随机抖动）CDP 打开 Boss 消息页
  -> 被动捕获 chat list API 响应 -> 解析未读/最新消息
  -> 新消息入册 message_events（job_id 关联、方向、摘要、时间）
  -> registry.mark(hr_replied)（确定性代码）
  -> 通知用户（Telegram / 桌面通知）
  -> Agent 生成回复草稿（基于 JD + journey 状态 + 招呼语历史）
  -> [HITL] 用户确认后发送（第一阶段不自动发送）
```

### 切片

| 切片 | 内容 | 风险 | 验收 |
|---|---|---|---|
| HG-1 | `BossMessageMonitor`：被动捕获消息列表 + 新消息检测 + `message_events` 表 + registry 联动 `hr_replied` | 低（只读） | 同一消息不重复入册；风控冷却复用 `get_boss_cooldown` |
| HG-2 | 通知：新消息/新状态变化推 Telegram（复用现有 notifier） | 零 | 用户不在 chat 也能知道 HR 回复 |
| HG-3 | 回复草稿：新消息触发 Agent 生成回复草稿 + 会话上下文摘要（该岗位 JD、历史沟通、已投简历版本） | 零 | 草稿可追溯引用 journey 数据 |
| HG-4 | HITL 发送：用户在 chat 中确认草稿后经 CDP 发送；默认关闭自动发送 | 高 | 发送前后消息状态确认；失败不重试 |
| HG-5 | `jobagent watch` CLI 命令 + 进程守护（单实例锁、崩溃恢复） | 低 | 重启后从游标续读，不漏不重 |

### 风险与原则

- 读消息列表也可能触发风控：低频 + 抖动 + 冷却共享，捕获不到 API 时安静退出本轮。
- 全自动回复 HR 长期不做（发送即代表候选人立场，错误代价高）；最多到"草稿 + 一键确认"。

---

## F5. 面试录音复盘 + 错题集 📋

**用户痛点**：每次面试的经验没有沉淀；同类题反复答错；面试表现无法量化改进。

### 架构

```text
录音文件（用户会中/会后提供，本地存放）
  -> InterviewSession 登记（关联 job_id、轮次、时间、形式）
  -> 转写（本地 Whisper 或 API）-> 结构化逐字稿（问题/回答/时间戳）
  -> 面试题提取 + 错题判定（答错/答差/没答上）
  -> 错题集 WrongAnswerBook（跨面试聚合：题目、我的回答、更好回答、知识点、来源）
  -> 多维度打分（技术深度/表达/匹配度/沟通；LLM 评分必须引用逐字稿证据）
  -> 经验贴草稿（可追溯）+ 改进方向 -> 反哺 Interview Preparation Pack
```

### 切片

| 切片 | 内容 | 风险 | 验收 |
|---|---|---|---|
| IV-1 | InterviewSession 模型 + 录音文件登记（内容哈希、本地路径、metadata 入库，关联 registry job_id） | 零 | 同一录音不重复登记 |
| IV-2 | 转写：本地 whisper（faster-whisper）优先，API 兜底；输出带时间戳结构化逐字稿（engine/model/version 记录，对齐 Image Content Extraction 的 provenance 原则） | 低 | 逐字稿可追溯到原始录音 |
| IV-3 | 面试题提取 + 错题集：`wrong_answer_book` 表，跨面试聚合，每题记录我的回答/更好回答/知识点/来源面试 | 零 | 准备包生成时自动包含相关错题复习 |
| IV-4 | 多维度打分：维度可配置，评分必须引用逐字稿原文位置；输出雷达图数据 + 文字总结 | 低 | 无证据支撑的分数无效 |
| IV-5 | 经验贴生成 + 改进方向：按 Raw Source Snapshot 理念可追溯；改进方向关联下次准备包 | 低 | 每个结论可回溯到逐字稿 |

### 风险与原则

- 录音涉及第三方（面试官）声音：本地处理优先，默认不外传；使用 API 转写需用户显式选择。
- 错题集是长期资产：入库即不可原地覆盖，修订走版本。

---

## 推荐实施顺序

```text
F3 TR-1/TR-2（简历版本库，面试刚需，零风险）
  -> F4 HG-1/HG-2（消息监控 + 通知，信息流入口）
  -> F5 IV-1/IV-2（面试记录 + 转写）
  -> F3 TR-3/TR-4（Boss 站内简历/PDF 写操作，需真机校准）
  -> F4 HG-3/HG-4（回复草稿 + HITL 发送）
  -> F5 IV-3~IV-5（错题集/打分/经验贴）
  -> F2 G1/G2、F1 R1~R4 穿插进行
```

排序依据：先解决"面试时信息对齐"（TR-1/2）和"信息不漏"（HG-1/2）这两个高价值低风险
痛点；平台写操作（TR-3/4、HG-4）风险最高、依赖真机校准，放后并全部 HITL。
