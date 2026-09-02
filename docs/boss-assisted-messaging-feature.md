# Boss 辅助沟通 Feature

> 状态：Proposed
> 范围：Boss 岗位首次打招呼、已有 HR 会话回复
> 核心决策：Agent 完成沟通准备，用户执行最终发送

## 1. 背景

JobAgent 需要帮助用户在 Boss 上联系招聘者，但当前浏览器自动发送会触发平台风控：聊天页可能被导航到 `about:blank`，页面执行上下文随之销毁。直接连接平台私有 MQTT/WebSocket 虽然技术上可行，但存在账号限制、封禁和平台协议风险，不能作为主账号的默认生产通道。

因此，本 Feature 采用辅助沟通模式：Agent 自动完成岗位和会话定位、上下文分析、文案生成及本地交付，用户检查后执行最后一次粘贴和发送。

## 2. 目标

- 将“找岗位、找 HR、阅读上下文、组织语言”的重复工作交给 Agent。
- 同时支持针对新岗位首次打招呼，以及回复已有 HR 会话。
- 每条消息在发送前都向用户展示完整目标和文案。
- 将最终的平台写操作保留给用户，避免使用私有协议或高风险页面自动化。
- 持久记录草稿、人工确认和后续跟进状态，但不虚报发送成功。

## 3. 非目标

- 不通过私有 MQTT/WebSocket 自动发送消息。
- 不使用 Playwright/CDP 在 Boss 聊天页自动输入或点击发送。
- 不绕过验证码、设备校验、频率限制或其他平台风控。
- 不根据“已打开页面”或“已复制文案”推断消息已经发送。
- 不做无人值守批量打招呼或群发。

## 4. 用户体验

### 4.1 首次打招呼

用户可以说：

```text
帮我联系这个岗位的 HR：<Boss 岗位 URL>
```

JobAgent：

1. 读取岗位、公司、招聘者和 Candidate Background。
2. 判断该岗位是否值得联系，并检查历史投递与重复沟通记录。
3. 生成针对该岗位的个性化招呼语。
4. 展示目标岗位、公司、招聘者和完整文案，请用户确认。
5. 用户确认后，打开准确的岗位或沟通页面，并将文案复制到剪贴板。
6. 提示用户粘贴、检查并点击发送。
7. 用户回到 JobAgent 确认“已发送”“未发送”或“内容有修改”。

### 4.2 回复已有 HR

用户可以说：

```text
读一下夏女士的聊天记录，帮我准备回复。
```

JobAgent：

1. 读取已获准使用的会话上下文或本地聊天快照。
2. 总结对方意图、未回答问题和当前 Opportunity Journey 状态。
3. 基于 Candidate Background 起草回复，不虚构经历和能力。
4. 展示收件人、上下文摘要和完整文案，请用户确认。
5. 用户确认后，打开对应会话，并将文案复制到剪贴板。
6. 用户执行最终发送，然后回到 JobAgent 确认结果。

### 4.3 页面无法准确定位

如果系统不能生成稳定的岗位或会话链接：

- 仍将文案复制到剪贴板。
- 展示招聘者姓名、公司、职位和最后一条消息，供用户手动搜索。
- 返回 `navigation_unavailable`，不得降级为自动操作页面。

## 5. Agent Tools

### 5.1 `prepare_boss_greeting`

为一个具体 Job Posting 准备首次招呼。

```python
prepare_boss_greeting(
    job_url: str,
    user_confirmed: bool = False,
) -> BossMessagePreparationResult
```

约束：

- `job_url` 必须指向明确的 Boss 岗位。
- 必须检查重复沟通、历史投递与 Application Eligibility。
- `user_confirmed=false` 时只返回草稿，不打开页面、不修改剪贴板。
- `user_confirmed=true` 时只执行本地交付，不发送平台消息。

### 5.2 `prepare_boss_reply`

为已有 HR 会话准备回复。

```python
prepare_boss_reply(
    hr_name: str,
    user_instruction: str | None = None,
    user_confirmed: bool = False,
) -> BossMessagePreparationResult
```

约束：

- 招聘者姓名存在歧义时必须结合公司、职位或会话标识消歧。
- 只能使用已有 Candidate Background 和用户明确提供的事实。
- 必须保留 HR 原问题，避免生成答非所问的通用模板。
- `user_confirmed=true` 仍不代表消息已经发送。

### 5.3 `confirm_boss_message_sent`

记录用户执行最终发送后的结果。

```python
confirm_boss_message_sent(
    preparation_id: str,
    outcome: Literal["sent", "not_sent", "sent_with_edits"],
    final_message: str | None = None,
) -> BossMessageConfirmationResult
```

约束：

- 只有用户明确确认后，状态才能变为 `USER_CONFIRMED_SENT`。
- `sent_with_edits` 必须保存最终发送文本，保证后续回复基于真实上下文。
- 未确认的草稿不能写入 Opportunity Journey 的已发送沟通记录。

## 6. 状态模型

```text
DRAFTED
  -> WAITING_USER_APPROVAL
  -> READY_FOR_USER
      -> USER_CONFIRMED_SENT
      -> USER_CONFIRMED_NOT_SENT
      -> EXPIRED
```

状态含义：

| 状态 | 含义 |
|---|---|
| `DRAFTED` | 已生成草稿，尚未展示或确认 |
| `WAITING_USER_APPROVAL` | 已展示完整目标和文案，等待用户确认 |
| `READY_FOR_USER` | 页面已打开或定位信息已展示，文案已复制 |
| `USER_CONFIRMED_SENT` | 用户明确确认已经发送 |
| `USER_CONFIRMED_NOT_SENT` | 用户取消或确认没有发送 |
| `EXPIRED` | 草稿超时，发送前需要重新检查上下文 |

系统不得自动产生 `SENT` 状态。

## 7. 数据结构

```python
class BossMessagePreparation(BaseModel):
    preparation_id: str
    kind: Literal["greeting", "reply"]
    status: Literal[
        "DRAFTED",
        "WAITING_USER_APPROVAL",
        "READY_FOR_USER",
        "USER_CONFIRMED_SENT",
        "USER_CONFIRMED_NOT_SENT",
        "EXPIRED",
    ]
    hr_name: str | None
    company_name: str | None
    job_title: str | None
    job_url: str | None
    conversation_reference: str | None
    context_summary: str
    message: str
    copied_to_clipboard: bool
    navigation_url: str | None
    created_at: datetime
    expires_at: datetime
    final_message: str | None
    user_confirmed_at: datetime | None
```

`BossMessagePreparation` 是一次辅助沟通 Task Run 的产物；它不能代替真实的聊天记录，也不能覆盖原始会话快照。

## 8. 返回结果

准备成功：

```json
{
  "status": "ready_for_user",
  "preparation_id": "bmp_...",
  "to": "夏女士",
  "company": "示例公司",
  "job_title": "大模型应用开发工程师",
  "message": "您好……",
  "copied_to_clipboard": true,
  "navigation": "opened",
  "next_action": "请粘贴并检查文案，然后点击发送"
}
```

无法定位页面：

```json
{
  "status": "ready_for_user",
  "error_type": "navigation_unavailable",
  "preparation_id": "bmp_...",
  "message": "您好……",
  "copied_to_clipboard": true,
  "search_hint": "夏女士 / 示例公司 / 大模型应用开发工程师"
}
```

## 9. 安全和风控约束

- 不连接 Boss 私有 MQTT/WebSocket。
- 不对聊天页执行 `Runtime.evaluate`、自动输入或自动点击。
- 不自动重试页面打开；一次用户动作最多打开一次目标页面。
- 不附加用户正在使用的 Boss 页面进行 CDP 调试。
- 不在日志、Artifact 或 LLM 上下文中暴露 Cookie、`bst`、`zp_token`、`securityId` 或 WebSocket 子协议。
- 不提供批量操作参数；一次 Preparation 只对应一个岗位或一个招聘者会话。
- 默认草稿有效期为 30 分钟；超时后必须重新读取上下文。
- 用户取消后不得继续执行任何外部或本地交付动作。

## 10. 与现有自动发送工具的关系

- `reply_boss_greeting` 不再由正常 Agent 路径调用。
- 私有 WS 和 UI 自动发送实现仅保留为禁用的实验代码，不能自动 fallback。
- 正常对话分别路由到 `prepare_boss_greeting` 或 `prepare_boss_reply`。
- 迁移期间如果旧工具被调用，应返回 `automatic_send_disabled`，并提示改用辅助沟通工具。

## 11. 验收标准

### 首次打招呼

- 给定一个有效 Job Posting，系统能生成包含岗位相关信息的非模板化招呼语。
- 未经确认时，不打开浏览器、不修改剪贴板、不记录已发送。
- 用户确认后，系统最多打开一次页面并复制一次文案。
- 同一岗位已有有效沟通记录时，系统阻止重复招呼并说明原因。

### 回复 HR

- 回复内容能直接回答 HR 的最后问题，并引用正确的候选人事实。
- 同名招聘者存在多条会话时，系统不猜测目标。
- 页面无法定位时仍能安全交付文案，不触发自动化 fallback。
- 用户修改后发送时，最终文本能够被持久记录。

### 状态与诚实性

- `READY_FOR_USER` 不被展示为“已发送”。
- 只有 `confirm_boss_message_sent(..., outcome="sent")` 能产生 `USER_CONFIRMED_SENT`。
- 进程重启后仍能查询未确认的 Preparation。
- 过期草稿不能直接确认发送，必须重新准备或重新确认内容。

### 风控

- 正常流程不创建 WebSocket 连接、不执行聊天页 evaluate、不自动按键。
- 页面打开失败时不重试，不调用旧 UI/WS sender。
- 所有测试使用 Fake Browser、Fake Clipboard 和本地状态存储，不接触真实 Boss 账号。

## 12. 交付阶段

1. `BossMessagePreparation` 模型和持久化存储。
2. `prepare_boss_reply`：已有会话回复草稿与 HITL。
3. `prepare_boss_greeting`：岗位首次招呼与重复沟通检查。
4. Browser/Clipboard 本地交付 Adapter。
5. `confirm_boss_message_sent` 与 Opportunity Journey 沟通记录。
6. Agent 路由迁移，禁用自动 WS/UI fallback。

## 13. 成功指标

- 用户从提出沟通请求到获得可发送文案不超过一次确认。
- 正常流程的平台自动写操作数量为零。
- 不再出现 `typing_failed`、`rpc_failed` 被 fallback 覆盖的误导结果。
- 每条标记为已发送的消息都能追溯到一次明确的用户确认。
- 用户只需执行最终粘贴、检查和点击发送。
