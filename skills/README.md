# Skills（技能）

JobAgent 的 skill 系统设计为**跨 Agent 可移植**，支持标准目录形态
（`SKILL.md` + 附属 `scripts/` / `references/` / `assets/` 文件）。

## 设计原则

- **SKILL.md 是入口**——frontmatter 声明 `name`/`description`，正文是工作流指令；
  目录可携带脚本、参考文档和静态资产
- **只用通用工具**——步骤通过 `execute`（Shell 命令）、`read_file`、`ls`、
  `write_file` 等所有 Agent 都具备的通用工具描述；携带的脚本同样经
  `execute` 的审批路径执行，安装器自身不导入、不执行任何代码
- **跨 Agent 可移植**——同一个 skill 目录可以直接用在 Pi、Claude Code、
  OpenClaw 或其他支持通用工具的 Agent 上，反之亦然

## 创建新 skill

```
my-skill/
├── SKILL.md          # 必需：frontmatter + 工作流指令
├── scripts/          # 可选：脚本（.py/.js 等，模型经 execute 调用）
├── references/       # 可选：参考文档
└── assets/           # 可选：静态资产（图片等）
```

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
- `read_skill`：读取 Skill 指令，返回中含 `path`（skill 目录绝对路径，
  模型可据此执行目录内脚本）
- `install_skill`：从本地 skill 目录、本地 `SKILL.md` 文件或 HTTPS URL
  安装 Skill；目录安装会拷贝全部附属文件，返回值含 `files` 清单

Skill 不会被导入为 Python 代码；携带的脚本由模型经 `execute` 工具的
审批路径执行。安装操作会经过人工批准，并校验：

- YAML frontmatter（`name`/`description`）与名称格式
- `SKILL.md` ≤ 512 KiB，整目录 ≤ 5 MiB
- 目录中不允许符号链接、隐藏文件（`.` 开头）和不可审计的二进制文件
（`.exe`/`.dll`/`.so` 等）；明文脚本（`.sh`/`.py` 等）允许携带，
由 `execute` 审批路径把关


### 渠道工作流 Skill

`xhs-recruitment-email` 这类渠道工作流 Skill 引用 JobAgent 业务 Tool（如
`analyze_recruitment_note`），不满足上面的通用工具可移植准则，因此单独归类：
它在 `build_job_agent` 装配时被**确定性注入**所属 Subagent 的 system prompt
（剥掉 frontmatter，仅注入正文），不依赖模型主动读取；Subagent 同时持有只读
`read_skill`，用于会话中途安装的新版本或读取其他 Skill。`install_skill` 是
HITL 写操作，仅主 Agent 持有。
