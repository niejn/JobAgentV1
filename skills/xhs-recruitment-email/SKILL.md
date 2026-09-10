---
name: xhs-recruitment-email
description: "小红书招人帖邮件投递全流程：保存帖 → 核对岗位与邮箱 → 核对附件 → 生成草稿 → 人工审批发送。"
author: JobAgent
version: 1.0.0
---

# 小红书招人帖邮件投递流程

固定顺序执行，不得跳步，不得并行外发。每个工具都会校验上一步的落库产物：
跳步只会拿到结构化失败并浪费一轮，按本流程走是唯一能走通的调用序列。

## 开始前核对 task 上下文

task 任务描述是唯一上下文来源（你不继承主对话历史），必须包含：

- 帖子 URL 或 note_id
- 已确认的公司名与岗位
- Journey ID（如有）
- 附件简历文件名
- 用户已逐字确认的邮件主题/正文（如有）

缺帖子内容 → 用提供的 URL 执行步骤 1；缺公司或简历 → 返回 blocked 并列出
缺失项，不要臆造，不要自行推断后当作"用户已确认"。

## 步骤 1 — 保存帖子

`analyze_recruitment_note(url)`：抓取帖子并落库，返回 note_id、candidate_positions、
contacts。task 已提供 note_id 且无内容变化信号时跳过本步直接复用。

## 步骤 2 — 核对岗位与邮箱

`select_recruitment_position(note_id, position_index, company)`：
company 必须来自用户确认，不得从帖子文本自行推断。

邮箱核对标准：只接受 contacts 中 confidence == "verified" 的条目（来自帖子
正文或图片 OCR）。作者评论里的邮箱是 review_required，永远不能进入草稿。
对邮箱拼写有疑虑（如 zhiqiu/zhigiu）时，在返回中说明帖子各来源分别出现了
哪些地址，交由用户裁决。

## 步骤 3 — 核对附件

`list_available_resume_pdfs()`：确认简历文件名在受控简历库中。文件不在库中 →
返回 blocked，请用户在主对话提供路径注册；不要扫描本地目录，不要改用其他文件。

## 步骤 4 — 生成草稿

`prepare_recruitment_email(note_id, resume_file_name, sender_name, subject?, body_text?)`：

- 用户已逐字确认主题/正文 → 原样传入 subject 和 body_text，一字不改
- 未确认 → 省略 subject/body_text，使用默认模板

草稿回执含帖子链接、邮箱出处（body/image_ocr 与置信级别）、附件 SHA-256，
原样呈报给主对话供用户审阅。

## 步骤 5 — 审批发送

`send_recruitment_email(draft_id)`：触发人工审批暂停，等待 approve/reject。
批准前不得重复调用；回执如实上报 submitted / unverified / failed / blocked。

## 失败恢复表

| error_type | 含义 | 动作 |
|---|---|---|
| note_not_saved | 帖子未保存 | 回步骤 1 |
| position_not_selected | 岗位未选定 | 回步骤 2（company 需用户确认） |
| contact_missing | 无 verified 邮箱 | 返回 blocked，说明帖子内联系方式情况 |
| resume_not_found | 简历不在受控库 | 返回 blocked，请用户提供 |
| draft_not_found | 草稿不存在 | 回步骤 4 |
| already_submitted | 该草稿已投递 | 如实返回回执，不重发 |

## 禁止事项

- 不扫描本地目录、不猜测简历路径
- 不改写用户确认的邮件内容
- 一次任务最多准备和执行一项外发动作
- 不绕过审批、不预测审批结果
