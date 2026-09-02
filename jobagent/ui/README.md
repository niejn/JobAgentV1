# JobAgent 前端原型

当前 `ui/` 是 React/Vite 前端，信息架构以 `OpportunityJourney` 为中心。正式桌面路线采用 React/Vite + FastAPI/Uvicorn + pywebview + PyInstaller，架构决策见 [`docs/adr/0001-desktop-runtime-stack.md`](../docs/adr/0001-desktop-runtime-stack.md)。

## 开发运行

在 `ui/` 目录安装依赖并启动 Vite：

```bash
npm install
npm run dev
```

 Vite 会把 `/api` 和 `/healthz` 代理到 `http://127.0.0.1:8008`。后端启动方式：

```bash
uvicorn jobagent.web:app --reload --port 8008
```

如果暂时没有启动后端，首页会自动使用演示数据；创建 Journey 需要后端在线。

## 从 CareerDesk 借鉴的功能

| CareerDesk 能力 | JobAgent 前端入口 | 当前状态 |
| --- | --- | --- |
| 求职进度面板 / Timeline | 求职旅程、机会空间 | Journey 列表已接入 `/api/journeys`，创建 Journey 已接入 SQLite |
| 公司与岗位调研 | Journey 任务 | 作为任务类型接 `job_discovery`、`job_description`，输出可验证产物 |
| 简历适配分析 / 匹配 | Journey 任务 | 作为任务类型接 `candidate_profile` 与匹配器 |
| 定制练习题 / 模拟面试 | Journey 任务 | 作为任务类型接 `interview`、小红书面经检索 |
| 求职智能助手 | 总览底部 AI 助手入口 | 已有入口，下一步接 Agent 流式会话 |
| 资料库与生成产物 | 后续可增加“资料库”导航 | 建议接 `artifacts` 与 `memory` |
| 设置、模型与隐私 | 设置 | 已预留入口，建议接 `config`、通知和出网策略 |
| 导入表格、时间线记录、复盘 | 岗位看板详情 | 建议第二阶段实现，减少手工录入成本 |

## 建议的接入顺序

1. 为 Python 运行层增加 HTTP/SSE API：岗位列表、创建岗位、状态更新、聊天流。
2. 用真实 `job_progress` 和 `job_discovery` 替换首页 mock 数据。
3. 增加岗位详情抽屉：JD、匹配分、分析报告、跟进记录、下一步行动。
4. 加入操作确认层：发送 Boss 消息、上传简历、投递邮件等外部写操作必须明确确认。
