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
| `xhs-recruitment-email` | 小红书招人帖邮件投递全流程（保存帖→核对→草稿→审批） |

## JobAgent 运行时

JobAgent 会自动发现内置 `skills/` 和 `JOBAGENT_SKILLS_DIR`（默认
`data/skills/`）中的标准 `SKILL.md`。对话中可使用：

- `list_skills`：列出有效 Skill 的名称和描述
- `read_skill`：读取 Skill 指令
- `install_skill`：从用户提供的本地 `SKILL.md` 或 HTTPS URL 安装 Skill


Skill 只能是文档指令，不会被导入或执行 Python 代码。安装操作会经过人工批准，
并校验 YAML frontmatter、名称格式和 512 KiB 大小上限。

### 渠道工作流 Skill

`xhs-recruitment-email` 这类渠道工作流 Skill 引用 JobAgent 业务 Tool（如
`analyze_recruitment_note`），不满足上面的通用工具可移植准则，因此单独归类：
它在 `build_job_agent` 装配时被**确定性注入**所属 Subagent 的 system prompt
（剥掉 frontmatter，仅注入正文），不依赖模型主动读取；Subagent 同时持有只读
`read_skill`，用于会话中途安装的新版本或读取其他 Skill。`install_skill` 是
HITL 写操作，仅主 Agent 持有。
