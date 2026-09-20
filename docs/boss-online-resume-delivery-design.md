# Boss 在线简历：列出与发送 — 需求与功能设计

状态：设计完成，待数据探测（G1–G4）后实施。日期：2026-09-20。
解码依据：`data/journeys/workspace/findings_online_resume.md`（Boss 前端 bundle 离线分析）。

## 背景（已验证事实）

- `checkbox.json?from=3`（prepare 预检数据源）只有三个能力开关
  （`supportCommonResume/supportAnnexType/supportVideoResume`）+ 附件 `resumeList`，
  **不含在线简历条目**。三个开关已由工具透传（commit 1cc04cd）。
> ## 抓包证据（2026-09-20 15:01–15:02，mitm 带登录态，G1–G4 已闭合）
>
> - 主动发送走 `POST /wapi/zpchat/exchange/request`：
>   `securityId + type=3 + encryptResumeId + mid=<可空>` → `{type:3,status:0}`。
>   （`_SEND_JS` 现用的 `exchange/accept` 是 HR 索要后的回复流，两者都通；
>   `exchange/test` 响应只有 `{alertType,type,status}`，不含简历条目——
>   原"checkInfo 含在线条目"假设证伪）
> - **`POST /wapi/zpgeek/resume/attachment/save.json` body `{}` → 返回新
>   `resumeId`**：在线简历同步生成附件的官方机制（G1 最终答案——无需
>   未观测到的弹窗 virtualResume.encryptId）
> - `GET /wapi/zpgeek/resume/sidebar.json`：附件列表 + 能力（在线简历存在性）
> - `GET /wapi/zpgeek/resume/geek/preview/data.json`：在线简历完整内容
>   （审批预览可展示关键信息）
> - `GET /wapi/zpgeek/resume/attachment/checkbox.json`：会话能力 flag + 附件
>
> ## 实施定稿
>
> send 的在线简历选项 = `attachment/save {}` 同步 → 用返回的新 resumeId 走
> exchange 发送；幂等键 `resume_file_name="在线简历(同步)"`。同步会新增一个
> 附件条目（Boss 自身行为一致），registry 幂等防止重复同步+重复发送。
- 弹窗组件 `choose-resume`：在线简历条目 = 父组件传入的 `virtualResume`
  （含 `encryptId`、`resumeName`、`uploadTime`），选中时
  `resumeId = resumeId || encryptId` 填入与附件相同的槽位。
- `exchange/accept` 接口只认 `{securityId, encryptResumeId, mid}`，
  不区分简历来源（video 分支直接在弹窗组件内调用证实）。
- 弹窗的 `secureExchange` 提示来自 `checkInfo` prop——**`checkInfo` 疑似就是
  `exchange/testAccept` 的响应体**，而现有 `_PREPARE_JS` 调用 testAccept 后
  只检查 `code==0`，丢弃了整个响应体（高价值假设，见 G1）。
- bundle 中另存在 `/wapi/zpchat/exchange/auth/accept`（授权交换）端点族，
  `annexType==3` 分支提示"您的最新简历将会发送给 BOSS，请在 BOSS 同意后查看"
  ——在线简历可能走授权流（见 G2）。

## 需求

- **R1 列出在线简历**：`prepare_boss_resume_after_hr_reply` 在
  `supportCommonResume=true` 的会话中列出"在线简历"选项，含名称、更新时间、
  可发状态；不可发时给出原因（restricted / secureExchange 提示）。
- **R2 发送在线简历**：用户在 HITL 审批中选择在线简历选项后，
  `send_boss_resume_after_hr_reply` 完成发送，回执与幂等语义和附件完全一致。
- **R3 审批预览**：HITL 预览显示"在线简历（更新于 …）"，与附件简历同格式。
- **R4 事后核验**：unverified 确认、幂等防重发、过期自动重备路径对在线简历同样生效。

非目标：在线简历内容编辑/预览渲染、视频简历发送、绕过 HITL 的任何路径。

## 功能设计

### 1. prepare 扩展（R1）

- 首选数据源（G1 验证后）：解析 `exchange/testAccept` 响应体的
  `zpData`（`checkInfo`/secureExchange 及可能的在线简历条目），不再丢弃。
- 备选数据源：聊天页其余 chunk 中 `virtualResume` 的赋值接口。
- 输出：`resume_options` 置顶插入
  `{resume_option_id: "online", file_name: "在线简历",
    resume_type: "在线简历", encrypt_resume_id, uploaded_at, selectable,
    restricted, restricted_reason}`。

### 2. send 扩展（R2）

- `resume_option_id="online"`：`_SEND_JS` 不变，`encryptResumeId` 传在线简历
  encryptId（G2 若确认走 `exchange/auth/accept`，则加端点分支，参数形状待 G4）。
- `resume_file_name="在线简历"` 作为登记册幂等键，天然与附件区分。
- HITL 审批预览、`ResumeDeliveryRegistry` 记录、过期重备、unverified 语义
  全部复用现有实现，无新状态机。

### 3. 边界与安全

- `secureExchange` 文案原样透传到审批预览（用户知情"BOSS 同意后可查看"类语义）。
- 出站护栏不适用（无自由文本）。
- 发送仍必须经 `send_boss_resume_after_hr_reply` 的 HITL 物理中断；
  agent 不得脚本直发（与附件发送同规矩，已写入 boss 子代理提示词）。

## 所需数据（缺口清单）

| # | 缺口 | 获取方式（全部只读） |
|---|---|---|
| G1 | 在线简历条目数据源（encryptId/resumeName/uploadTime/restricted）| **首选**：dump `exchange/testAccept` 完整响应体（改一个现有 probe 即可，可能一次填掉 G1+G3）；备选：其余 chunk grep `virtualResume` 赋值 |
| G2 | 发送端点确认：`exchange/accept` vs `exchange/auth/accept`（是否由 secureExchange 决定） | 解码聊天窗 chunk 中 `options.callback(resumeId)` 的消费处；或一次真实弹窗观测 |
| G3 | 在线简历元字段语义（restricted 在线简历是否存在、文案） | 随 G1 |
| G4 | `auth/accept` 参数形状与回执码语义（若 G2 指向它） | 解码 auth/accept 调用处上下文 |

## 实施切片

1. G1 探测（probe testAccept 响应）→ 若命中：prepare 解析扩展 + 测试（fake 响应加在线条目）
2. G2/G4 解码 → send 端点分支（若需要）+ 测试
3. 端到端：prepare 列出在线简历 → HITL 批准 → 发送 → 只读历史核验（人工验收一次）
4. 文档：`docs/future-work.md` 登记、boss 子代理提示词补一句"在线简历同样只走工具"

## 验收

- `supportCommonResume=true` 的会话 prepare 返回在线简历选项（含更新时间）
- 不可发时返回原因而非静默缺失
- 用户批准后发送成功，HR 侧收到在线简历（人工验收一次）
- 同会话同 mid 重复调用被幂等拦截（registry 键含"在线简历"）
- unverified / 过期重备路径行为与附件一致
