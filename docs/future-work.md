# Future Work

## 高优先级：平台招聘 Subagent 重构

在 Boss 主动发送简历功能完成后，将当前主 JobAgent 的平台底层能力按渠道收敛为两个
Subagent，主 JobAgent 只负责岗位匹配判断、Opportunity Journey 和跨渠道调度：

- `BossRecruitingAgent`：Boss 岗位发现、HR 会话、招呼、HR 回复后的简历发送和回执；
- `XhsRecruitingAgent`：小红书招人帖发现、JD/公开邮箱提取、定制邮件和附件简历投递；
- 主 Agent 不再直接持有 Boss token/会话、SMTP 或小红书抓取等平台私有工具；
- 两个 Subagent 统一交付 `JobDiscoveryResult`、`ResumeDeliveryProposal`、
  `DeliveryReceipt`，外部写操作仍通过 HITL 向用户展示渠道、收件人、岗位和简历文件名。

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
