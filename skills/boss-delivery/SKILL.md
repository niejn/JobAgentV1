---
name: boss-delivery
description: "Boss 直聘岗位投递全流程（HR 已回复阶段）：读会话确认 → 准备简历 → 人工审批发送 → 回执核验；含 ACK 丢失核验与失败恢复口径。"
author: JobAgent
version: 1.0.0
---

# Boss 岗位投递流程（HR 已回复阶段）

固定顺序执行，不得跳步，不得并行外发。一次任务最多准备和执行**一项**外发动作
（一条回复或一次投递）；每个工具都会校验上一步的落库产物，跳步只会拿到结构化失败。

## 开始前核对 task 上下文

task 任务描述是唯一上下文来源（你不继承主对话历史），必须包含：

- HR 姓名或 conversation/friend 标识、已确认的公司名与岗位
- Job ID / Journey ID（如有）
- 用户已确认的消息正文（回复场景）或选定的简历文件名（投递场景）

缺项 → 返回 blocked 并列出缺失字段，不要臆造，不要把推断当作"用户已确认"。

批量发现"谁在等我们回复"（如打招呼后的批量回执核验、找出该回复的 HR）用
`scan_boss_hr_replies`（只读、按活跃时间增量扫描），不要写脚本扫描。

## 步骤 1 — 读会话确认 HR 已回复

`read_boss_conversation(hr_name)`：确认最近一条是 HR 消息且值得回复；引用原文要点，
不要凭记忆转述。会话里没有 HR 回复 → 如实返回，不要进入投递。

## 步骤 2A — 回复 HR（可选外发动作）

`reply_boss_greeting(hr_name, message)`：触发人工审批暂停。

- message ≤ 500 字，以求职者本人第一人称书写（你就是用户本人）
- 含"测试/请忽略"话术或暴露 AI/自动化身份的文本会被出站护栏拒发（refused +
  违规原因）：换合格文案重写再发，不要原样重试

## 步骤 2B — 准备简历投递（可选外发动作）

`prepare_boss_resume_after_hr_reply(conversation_id)`：返回 delivery_id 和
resume_options（含 resume_option_id、file_name、restricted 状态）。

- Boss 简历**只**来自平台返回的选项，不读本地 PDF 库、不扫描目录
- restricted 的选项不可投；无可投选项 → no_sendable_resume，如实上报
- 把 resume_options 原样呈报主对话，由用户选定

## 步骤 3 — 审批发送

`send_boss_resume_after_hr_reply(delivery_id, resume_option_id, resume_file_name,
hr_name, company, job_title)`：触发人工审批暂停。

- resume_file_name 必须与 prepare 返回的 file_name 字段**逐字符一致**——包括平台侧
  的怪异命名（实测存在 xxx.pdfpdf 双扩展名）；不一致 → resume_filename_mismatch
- hr_name/company/job_title 必须与预检回执一致，否则 delivery_context_mismatch

## 回执口径

| status/error_type | 含义 | 动作 |
|---|---|---|
| confirmed | 平台 accept 与历史刷新双确认 | 上报回执，结束 |
| unverified / refresh_unconfirmed | 已发送但历史刷新未确认 | **不重发**；用 `read_boss_conversation` 读历史核验后如实改判 |
| resume_already_delivered | 同会话同简历已投过 | 幂等回执，如实上报，不重发 |
| cooldown | Boss 冷却中 | 上报预计等待分钟数，不重试 |
| delivery_expired* | HITL 批准超过预检 TTL | 工具已自动重新预检并按文件名回退匹配；选项失效时重新呈报新选项 |

## ACK 丢失铁律（实测 2026-09-22）

Boss 的 WS/MQTT PUBACK 经常丢失：**发送报错 ≠ 发送失败**。传输层会把已写出但
未确认的 publish 标记为歧义结果（携带解析好的会话目标），此时唯一正确动作是
读会话历史核验（`read_boss_conversation`）后再下结论——历史里出现了就是已送达。
绝不因为报错就自动重发；重发必须重新走人工审批。

## 通用铁律

- 所有外发经注册 Tool 的审批暂停完成；绝不写脚本 import `jobagent.applier.*`
  直发（会被执行守卫拒绝，且绕过审批属于事故）
- 回执状态（confirmed/unverified/failed/blocked）如实上报主对话，不美化
- upload_boss_resume_pdf 仅在用户明确要求更换附件简历时使用（需审批）
