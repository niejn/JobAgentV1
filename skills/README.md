# Skills（技能）

JobAgent 的 skill 系统设计为**纯文档、跨 Agent 可移植**。

## 设计原则

- **SKILL.md 是唯一内容**——不包含 `tool.py`、`get_tools()` 等自定义代码
- **只用通用工具**——步骤通过 `execute`（Shell 命令）、`read_file`、`ls`、`write_file` 等所有 Agent 都具备的通用工具描述
- **跨 Agent 可移植**——同一个 skill 可以直接用在 Pi、Claude Code 或其他支持通用工具的 Agent 上，反之亦然

## 创建新 skill

```markdown
---
name: my-skill
description: "简短描述这个技能做什么"
author: [你的名字]
version: 1.0.0
---

# 技能名称

## 工作流程

使用 `execute` 工具按步骤执行：

### 步骤 1 — 第一步描述

```bash
# 要执行的命令
```

### 步骤 2 — 第二步描述

根据上一步结果决定下一步行动。
```

## 已有 skill

| 目录 | 用途 |
|---|---|
| `ChromeCDP-setup` | 检测并启动 Chrome 远程调试端口 |