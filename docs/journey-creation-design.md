# CLI 与网页统一创建 Journey

## Journey 管理工具（CRUD）

Agent 注册六种能力：create_opportunity_journey、list_opportunity_journeys、
get_opportunity_journey、update_opportunity_journey、delete_opportunity_journey、
restore_opportunity_journey。列表支持公司过滤、offset/limit 分页和 include_deleted。
查询不审批；创建、修改、软删除和恢复均由框架 HITL 在写入前审批。

修改只接受公司、岗位、部门、招聘周期和 JD。省略字段保持不变，空字符串可清空可选字段，
公司/岗位不能清空，null 不合法。阶段变更不属于字段编辑，不能借此伪造投递事实。
修改 JD 保存旧版本。每次修改、删除、恢复记录前后内容、理由和时间，可通过详情查看最近100条历史。

每次变更必须指定具体 journey_id、查询获得的 expected_version、expected_company、
expected_role 和 reason。事务内再次核对，版本或身份变化返回冲突，必须重新读取并重新审批。
有运行/验证中任务时拒绝编辑和删除；软删除后禁止启动新任务。

删除为 deleted_at 软删除，默认列表隐藏，详情和 include_deleted 查询仍可查看，
不删除 JD、聊天、任务或产物。恢复也需要审批。创建去重键保留，重复创建已删除项
返回 deleted 状态，不新建或自动恢复。

清理重复项：先列表，再逐个详情比较 ID、部门、周期、JD 和任务/产物；说明保留与删除理由，
每个删除独立审批。仅同名不自动删除，没有自动合并关联产物，也没有硬删除接口。
历史真实数据不会因部署此功能而被删除或自动合并。

网页 API 共用 management 服务：GET /api/journeys、GET /api/journeys/{id}、
PATCH /api/journeys/{id}、DELETE /api/journeys/{id}、POST /api/journeys/{id}/restore。
变更请求体与 CLI 工具相同，路径 ID 须匹配请求 ID；404 表示不存在，409 表示状态/版本冲突。
当前网页仍以展示、创建为主，管理操作可从 chat 发起，切回网页会刷新列表。

## 用户流程与授权

在 chat 中关注、收藏、要求跟进或投递具体岗位时，Agent 应调用
`create_opportunity_journey` 提出创建请求。框架 HITL 在执行前展示公司、岗位、
来源岗位 ID 和已知 JD，逐项接受 approve/reject；模型不能用 user_confirmed 字段代替人。
拒绝创建不会撤销已批准的 HR 沟通，也不能作为拒绝后反复询问的理由。
无交互 CLI 沿用现有默认拒绝机制。

Journey 创建和打招呼、发邮件、发送简历是独立授权。创建始终初始为 targeted，
不表示已投递；greeted/default_greeting 也不代表定制招呼发送成功。
JD 可暂缺，必须返回 jd_missing 提示，不生成假 JD。

网页“确认并创建”即该次本地创建授权；与 CLI 调用同一个 creation 服务。
只有持久化成功、返回 journey_id 后才可以报告“网页可见”。

## 身份、重试和已有数据

有 source_job_id 时以稳定 ID（如 boss:<encryptJobId>）去重；无 ID 时按精确的
公司、岗位、部门、招聘周期组合去重，不做公司简称模糊合并。
SQLite BEGIN IMMEDIATE 事务内完成去重、Journey/JD 写入与 creation key 绑定。
重复调用返回既有 ID，不重写已有 JD、阶段或人工编辑字段。
不同来源 ID 不自动合并；缺少来源 ID 的历史记录不自动归并，避免把不同招聘机会合为一条。

job_records 仍记录沟通进度；journey_creation_keys 将 source_job_id 与 Journey 关联。
本次不自动把历史 greeted 岗位批量导入：每个岗位仍需用户创建授权。
现有历史重复 Journey 保持原样。

## 页面同步

网页加载及重新获得焦点时读取真实 /api/journeys，独立于 profile 请求。
请求失败保留已加载列表，不注入示例卡片；返回原有 Journey ID 时不重复显示卡片。
CLI 和后端须配置同一个 JOBAGENT_STATE_DB，并从同一工作目录启动（推荐使用绝对路径）。

## 验收与范围

自动化测试覆盖：真实 HITL 暂停前不写库、拒绝不创建、批准后网页查询同一 ID、
CLI/Web 重试去重、并发只创建一条、JD 暂缺、不同岗位 ID 隔离。
提示词负责在关注/投递语义下发起创建工具；没有增加“发消息成功就绕过审批自动创建”的路径。
后续可单独完善旧记录人工关联、Journey 阶段与实际投递回执的同步，以及对长期拒绝的偏好记录。
