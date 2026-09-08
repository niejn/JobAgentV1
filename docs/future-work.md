# Future Work

## 低优先级：网页端招聘任务与投递回执看板

当前平台招聘 Subagent 的交互、HITL 批准和 `Command(resume=...)` 恢复仅由 CLI 承担。
网页前端不参与审批、不会持有 Agent `thread_id`，也不直接恢复被中断的 Agent。

未来在 Opportunity Journey 详情页增加只读看板：

- 展示 Boss/XHS 子 Agent 的任务进度、失败原因和最终状态；
- 展示已持久化的 `DeliveryReceipt`：渠道、收件人引用、岗位、简历/消息版本、确认状态和时间；
- CLI 正在等待审批时仅显示“等待 CLI 人工确认”，不提供网页批准按钮；
- 在 CLI 闭环稳定且有真实跨设备查看需求后，再评估网页审批与恢复是否值得单独设计。

该功能不属于当前 CLI 招聘闭环的验收范围。

## 高优先级：平台招聘 Subagent 重构

在 Boss 主动发送简历功能完成后，将当前主 JobAgent 的平台底层能力按渠道收敛为两个
Subagent，主 JobAgent 只负责岗位匹配判断、Opportunity Journey 和跨渠道调度：

详细设计见 `docs/platform-subagent-rearchitecture-design.md`。

- `BossRecruitingAgent`：Boss 岗位发现、HR 会话、招呼、HR 回复后的简历发送和回执；
- `XhsRecruitingAgent`：小红书招人帖发现、JD/公开邮箱提取、定制邮件和附件简历投递；
- 主 Agent 不再直接持有 Boss token/会话、SMTP 或小红书抓取等平台私有工具；
- 两个 Subagent 交付 `JobDiscoveryResult`、短时 `ChannelActionDraft` 和最终
  `DeliveryReceipt`；外部写操作仍通过 HITL 向用户展示渠道、收件人、岗位和简历文件名。

## 高优先级：Boss HR 消息后台监控与受控自动回复

目标：JobAgent 常驻后台定时读取 Boss HR 新消息，识别需要候选人回应的问题，并在严格
事实边界内处理简单、低风险问答；复杂或未确认信息必须进入人工审批队列。

首批支持的问题类别：

- 期望薪资；
- 到岗时间；
- 在职/离职状态；
- 是否可到公司现场面试；
- 已确认的城市、工作方式和基础沟通安排。

流程：

```text
定时读取 Boss 会话增量消息
  -> 消息去重与 HR/岗位关联
  -> 意图分类、事实检索、置信度与风险判定
  -> 生成简短回复草稿
  -> 白名单事实 + 用户预先允许的自动回复策略：发送并记录回执
  -> 其他情况：写入待回复队列，展示 HR/岗位/原问题/草稿，等待 HITL 批准
  -> 发送后读取会话回执、审计并更新 Journey
```

安全与产品约束：

- 不从简历或历史对话猜测薪资、到岗日期、离职状态、面试可用时间；只能使用用户确认的
  `Job Search Profile`、候选人事实和显式回复策略；
- 默认不自动发送，先以草稿 + HITL 模式上线；用户可按问题类别、公司或 HR 明确开启
  自动回复；
- 涉及薪资谈判、offer、入职承诺、面试时间确认、个人联系方式、附件简历、拒绝/接受岗位
  或任何低置信度问题，永远要求人工批准；
- 按会话 ID + 来源消息 ID 幂等，失败/回执不明不得自动重试；
- 后台轮询必须使用已登录 Chrome 页面内接口、共享 Boss 限流和风控冷却，不做高频刷新。

建议实现切片：

1. `BossMessageMonitor` 增量轮询、消息归档和去重；
2. `HrQuestionClassifier` 和只读事实解析；
3. `HrReplyPolicy`（默认草稿、按类别/公司/HR 的自动回复授权）；
4. `BossReplyQueue` 持久化待回复、审批、发送、回执和失败状态；
5. Agent/网页工具：查看待回复、批准草稿、修改后发送、配置自动回复策略；
6. 后台 worker 生命周期、限流、冷却、告警与可观测性。

## 已实现：损坏多模态 checkpoint session 的兼容处理

- 文本模型调用时只生成 request 级别的消息投影，不删除 checkpoint 中的原始图片。
- 模型明确返回不支持图片的 400 后，按“模型名 + Base URL”记录为 `text`，并清洗后重试一次。
- 悬空并发 tool call 补充取消结果并写回 checkpoint，不自动重放旧工具。
- `invalid_tool_calls` 和孤立 `ToolMessage` 在恢复时清理。
- 如果 checkpoint 本身无法反序列化，仍需要单独的数据库恢复工具。

## 小红书招人帖到 HR 邮件投递

状态：已登记，待实施。需求设计见
`docs/xhs-recruitment-lead-to-email-requirements.md`。

样本来源：

- [创业团队/Agent 招聘帖](https://www.xiaohongshu.com/discovery/item/6a82b8df0000000025014840)
- [Moodio 多岗位招聘帖](https://www.xiaohongshu.com/explore/6a2610dd000000001702d446)

目标流程：

```text
小红书招人帖
  -> 正文/图片/评论采集
  -> 招聘帖识别
  -> 图片 OCR/视觉解析
  -> 多岗位 JD 拆分
  -> 正文/图片/评论邮箱提取
  -> 招聘线索归档
  -> 岗位定制简历
  -> HR 自我介绍邮件草稿
  -> HITL 确认收件人/正文/附件
  -> SMTP 投递与回执审计
```

当前已有：

- `save_shared_url` 支持两类完整小红书笔记 URL；
- 可保存正文、原始响应和图片；
- `extract_shared_url` 可读取正文并执行图片 OCR；
- 用户提供的完整 URL 必须保留 `xsec_token`，不能使用裸 note_id。

待开发切片：

1. 评论抓取、快照归档和按评论 ID 去重；
2. 招聘帖分类器与不确定结果人工复核；
3. 正文/图片/评论中的 JD 证据提取；
4. 多岗位拆分为独立 Job Lead；
5. 投递邮箱提取、来源记录和可信度分级；
6. 基于已确认候选人事实生成岗位定制简历；
7. 生成有岗位针对性的 HR 邮件草稿；
8. 邮件收件人、正文、附件和简历版本 HITL 确认；
9. SMTP 发送、幂等、防重复、Message-ID 和失败恢复；
10. Agent Tools：`find_recruitment_posts`、`extract_recruitment_leads`、
    `prepare_recruitment_email`、`send_recruitment_email`。

安全边界：不猜测邮箱、不把普通评论当官方 JD、不自动评论或私信、不把 SMTP 成功说成 HR 已读，
所有简历邮件外发都必须经过人工确认。
