---
name: omp-volcengine-switch
description: "在 Oh My Pi (OMP) 中一键切换 Volcengine 火山方舟 Coding Plan 与 Agent Plan 配置"
author: JobAgent
version: 1.0.0
---

# OMP 火山方舟 Plan 切换

## 目标

Oh My Pi (OMP) 当前仅支持单 LLM Provider 配置。本 Skill 用于在以下两种火山方舟
Plan 配置之间切换，并安全地备份、修改 `.env`，避免误改密钥。

## 支持的 Plan

| Plan | 用途 | Base URL (OpenAI 兼容) | 典型模型 |
|---|---|---|---|
| Coding Plan | 代码生成、补全、Agent 编程 | `https://ark.cn-beijing.volces.com/api/coding/v3` | `glm-5.3`、`doubao-seed-2.0-code`、`deepseek-v4` |
| Agent Plan | 长流程 Agent 任务 | `https://ark.cn-beijing.volces.com/api/agent/v3` | 与 Agent 场景匹配的模型 |

> 注意：Coding Plan 与 Agent Plan 的 Key 是**不同套餐的专用 Key**，不可混用。
> 同一 Key 在 Claude 中可用，不代表能用于 OMP 的另一个 Plan。

## 前置条件

- OMP 已安装，且 `.env` 位于 `C:\Users\<用户名>\.omp\agent\.env`
- 已分别购买/开通 Coding Plan 与 Agent Plan，并各自持有专用 API Key
- 本 Skill 仅修改 `.env`，不修改 OMP 代码

## 工作流程

### 步骤 1 — 检查当前配置

```powershell
Get-Content "$env:USERPROFILE\.omp\agent\.env" | Select-String -Pattern "OPENAI_BASE_URL|OPENAI_API_KEY|JOBAGENT_LLM"
```

记录当前值（Key 只输出前 4 位与后 2 位，严禁明文外泄）：

```powershell
$env = Get-Content "$env:USERPROFILE\.omp\agent\.env"
$env | Select-String "OPENAI_BASE_URL|OPENAI_API_KEY" | ForEach-Object {
  $line = $_.Line
  if ($line -match "KEY") {
    $line -replace "(^.{4}).*(.{2}$)", '$1...$2'
  } else { $line }
}
```

### 步骤 2 — 选择目标 Plan

- 要切换至 **Coding Plan**：
  ```powershell
  Set-Content -Path "$env:USERPROFILE\.omp\agent\.env" -Value (
    (Get-Content "$env:USERPROFILE\.omp\agent\.env") -replace
    '^(OPENAI_BASE_URL=).*','$1https://ark.cn-beijing.volces.com/api/coding/v3' -replace
    '^(OPENAI_API_KEY=).*','" + $codingKey + '"'
  )
  ```
  同时将 `JOBAGENT_LLM_MODEL` 设为目标模型（如 `glm-5.3`）。

- 要切换至 **Agent Plan**：
  ```powershell
  Set-Content -Path "$env:USERPROFILE\.omp\agent\.env" -Value (
    (Get-Content "$env:USERPROFILE\.agent\agent\.env") -replace
    '^(OPENAI_BASE_URL=).*','$1https://ark.cn-beijing.volces.com/api/agent/v3'
  )
  ```
  并将 `OPENAI_API_KEY` 替换为 **Agent Plan 专用 Key**。

> 执行前务必确认 Key 与 Plan 一一对应：Coding Plan Key → `/api/coding/v3`，
> Agent Plan Key → `/api/agent/v3`。混用必报 `401 The API key format is incorrect`。

### 步骤 3 — 验证配置

```powershell
Get-Content "$env:USERPROFILE\.omp\agent\.env" | Select-String "OPENAI_BASE_URL|OPENAI_API_KEY|JOBAGENT_LLM_MODEL"
```

确认：
- Base URL 与所选 Plan 匹配
- Key 前缀与 Plan 匹配（Coding Plan 通常以 `sk-sp-` 开头，Agent Plan Key 格式以控制台显示为准）
- `JOBAGENT_LLM_MODEL` 是该 Plan 实际支持的模型（如 `glm-5.3` 需在 Coding Plan 中开通）

### 步骤 4 — 重启 OMP

关闭当前 OMP 会话后重新启动，使 `.env` 生效。若仍报 401，执行步骤 5。

### 步骤 5 — 401 诊断

若 `401 The API key format is incorrect` 仍存在：

| 现象 | 处理 |
|---|---|
| Base URL 含空格或尾随斜杠 | 删除空格，URL 末尾不加 `/` |
| 使用通用 `sk-...` Key | 回到火山控制台重新生成该 Plan 的专用 Key |
| Key 与 URL 套餐不匹配 | 按步骤 2 重新切换 |
| Claude 可用但 OMP 不可用 | OMP 读的是 `C:\Users\<用户名>\.omp\agent\.env`，确认未配置到其他文件 |

## 安全注意事项

- 不在日志、截图或对话中暴露完整 API Key
- 切换后立即重启 OMP，避免旧进程缓存旧配置
- 不将 `.env` 提交至 Git（已配置 `.gitignore`）
