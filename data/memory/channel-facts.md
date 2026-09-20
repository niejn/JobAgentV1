# Boss / 微信渠道事实（长期记忆）

> 跨会话共享的已验证结论。新事实追加到对应小节，标注日期与证据；不确定的不要写进来。

## Boss WS/MQTT 发送回执（2026-09-19 实测）

- WS/MQTT 回执经常丢失、连接中途关闭是常态，**不代表发送失败**
- 消息几乎总会送达，但要在会话历史里 **约 6 秒后** 才可见
- `unverified` = "无回执"，大概率已送达；绝不因 unverified 直接重发（会重复消息）
- 核验方式：只读会话历史（`read_boss_conversation` / historyMsg 接口）；
  `boss_greet_jobs` 批末会自动做只读二次核验并改判
  `history_confirmed_after_batch`

## Boss 简历发送弹窗机制（2026-09-20 离线解码前端 bundle `_app_71252f3c.js`）

- `checkbox.json?from=3` 的 `zpData` 只有：
  `complete, maxCount, resumeCount, resumeList, showUploadBtnType,
  supportAnnexType, supportCommonResume, supportVideoResume`
  —— 没有在线简历的 ID/内容，只有能力开关
- 弹窗组件 `choose-resume`：在线简历条目 = `virtualResume`（父组件传入，含
  `encryptId`）；选中走 `resumeId = t.resumeId || t.encryptId`（与附件同一槽位）
- video 分支直接调 `exchange/accept {securityId, encryptResumeId, mid}`——
  该接口只认 `encryptResumeId`，不区分附件/在线简历
- **发送在线简历 = 现有 exchange/accept 不变，encryptResumeId 换成在线简历的
  encryptId**（工具侧尚未支持，等 encryptId 来源接口确认）
- 剩余缺口：`virtualResume` 的来源接口在聊天页其他 chunk / 某
  `/wapi/zpgeek/resume/*` 中，主 bundle 只有 prop 定义
- 预检凭证 10 分钟时效：`send_boss_resume_after_hr_reply` 已支持过期自动重备
  （`reprepared_after_expiry`），不要为时效写脚本串联 prepare+send

## Boss 打招呼去重（2026-09-19/20 实测 + 工具已实现）

- 登记册去重键是 `(job_id, friend_id)`：同一 HR 换岗位链接会绕过——工具现已按
  平台聊天历史预检（`already_greeted_in_history`）+ per-HR 会话检查
  （`already_contacted_same_hr`）双层拦截，跳过即落库，下次走 already_contacted
- 任何发送类操作（打招呼/回复/发简历）必须走对应工具的 HITL 审批链；
  **绝不写脚本绕过审批**（脚本曾绕过 restricted 校验直接撞"用户权限限制"）

## 出站文本护栏（2026-09-19）

- 发往 Boss 的文本以求职者本人第一人称书写；测试性/请忽略话术与 AI/自动化身份
  表述会被出站护栏拒发（refused + 违规原因），重写后再发

## 微信 iLink（2026-09-19 实测）

- `get_qrcode_status` 是长轮询：服务器 hold ~30s，客户端预算需 ≥40s
- 微信网关只处理指令（/ping /status /help /progress /boss），自由文本回指引
- 同用户 5 分钟内相同自由文本会被内容去重（指令不受此限）
