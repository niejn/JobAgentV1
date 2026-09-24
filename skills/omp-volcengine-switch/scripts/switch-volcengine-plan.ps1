#!/usr/bin/env pwsw
<#
.SYNOPSIS
    OMP Volcengine Plan 切换脚本 - 在 Coding Plan 与 Agent Plan 之间一键切换
.DESCRIPTION
    根据用户选择更新 OMP 的 .env 配置，支持：
    - Volcengine Coding Plan: https://ark.cn-beijing.volces.com/api/coding/v3
    - Volcengine Agent Plan:  https://ark.cn-beijing.volces.com/api/agent/v3
.PARAMETER Target
    目标 Plan：coding 或 agent
.PARAMETER Key
    对应 Plan 的 API Key（如果通过参数提供）
.EXAMPLE
    .\switch-volcengine-plan.ps1 -Target coding -Key sk-sp-abcdef123456
.EXAMPLE
    .\switch-volcengine-plan.ps1  # 交互式询问
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)]
    [ValidateSet('coding','agent')]
    [string]$Target,
    
    [Parameter(Mandatory=$false)]
    [string]$Key
)

# 路径定义
$envPath = "$env:USERPROFILE\.omp\agent\.env"

if (-not (Test-Path $envPath)) {
    Write-Error "找不到 OMP 配置文件: $envPath"
    exit 1
}

function Get-CurrentConfig {
    Write-Host "`n=== 当前 OMP 配置（Key 已脱敏） ===" -ForegroundColor Cyan
    Select-String -Path $envPath -Pattern "^(OPENAI_BASE_URL|OPENAI_API_KEY|JOBAGENT_LLM_MODEL|JOBAGENT_LLM_PROVIDER)" |
        ForEach-Object {
            $line = $_.Line
            if ($line -match "^OPENAI_API_KEY=") {
                # 脱敏 Key：只显示前4位和后2位
                $keyPart = $line -split "=")[1]
                if ($keyPart) {
                    $visible = if ($keyPart.Length -le 6) { $keyPart } else { $keyPart.Substring(0,4) + "..." + $keyPart.Substring($keyPart.Length-2) }
                    Write-Host "OPENAI_API_KEY=$visible"
                } else {
                    Write-Host $line
                }
            } else {
                Write-Host $line
            }
        }
}

function Backup-Env {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupPath = "$envPath.bak.$timestamp"
    Copy-Path $envPath $backupPath
    Write-Host "已备份当前配置到: $backupPath" -ForegroundColor Green
}

function Update-Env {
    param(
        [string]$TargetPlan,
        [string]$NewKey
    )
    
    # 读取当前内容
    $content = Get-Content $envPath -Raw
    
    # 根据目标 Plan 配置 URL 和模型建议
    switch ($TargetPlan.ToLower()) {
        'coding' {
            $newUrl = "https://ark.cn-beijing.volces.com/api/coding/v3"
            $suggestedModel = "glm-5.3"
            $planName = "Coding Plan"
        }
        'agent' {
            $newUrl = "https://ark.cn-beijing.volces.com/api/agent/v3"
            $suggestedModel = "请根据 Agent Plan 实际支持的模型填写"
            $planName = "Agent Plan"
        }
    }
    
    # 替换 Base URL
    if ($content -match "(?m)^OPENAI_BASE_URL=") {
        $content = $content -replace "(?m)^OPENAI_BASE_URL=.*", "OPENAI_BASE_URL=$newUrl"
    } else {
        $content += "`nOPENAI_BASE_URL=$newUrl"
    }
    
    # 替换 API Key（如果提供了新 Key）
    if ($NewKey) {
        if ($content -match "(?m)^OPENAI_API_KEY=") {
            $content = $content -replace "(?m)^OPENAI_API_KEY=.*", "OPENAI_API_KEY=$NewKey"
        } else {
            $content += "`nOPENAI_API_KEY=$NewKey"
        }
    }
    
    # 建议更新模型（不强制，因为用户可能有偏好）
    if ($content -match "(?m)^JOBAGENT_LLM_MODEL=") {
        $content = $content -replace "(?m)^JOBAGENT_LLM_MODEL=.*", "# JOBAGENT_LLM_MODEL=$suggestedModel (请根据实际需要取消注释并设置)"
    } else {
        $content += "`n# JOBAGENT_LLM_MODEL=$suggestedModel (请根据实际需要取消注释并设置)"
    }
    
    # 写回文件
    Set-Content -Path $envPath -Value $content -Encoding UTF8
    
    Write-Host "`n已更新为 $planName 配置：" -ForegroundColor Green
    Write-Host "  Base URL: $newUrl"
    if ($NewKey) { Write-Host "  API Key: [已更新]" }
    Write-Host "  建议模型: $suggestedModel"
}

# 主流程
Get-CurrentConfig

# 确定目标 Plan
if (-not $Target) {
    Write-Host "`n请选择目标 Plan：" -ForegroundColor Yellow
    Write-Host "  1) Coding Plan (代码生成/补全)"
    Write-Host "  2) Agent Plan (长流程 Agent 任务)"
    
    $choice = Read-Host "请输入选项 [1/2]"
    switch ($choice) {
        '1' { $Target = 'coding' }
        '2' { $Target = 'agent' }
        default {
            Write-Error "无效选项"
            exit 1
        }
    }
}

# 获取 Key（优先使用参数，否则交互式询问）
if (-not $Key) {
    $planName = if ($Target -eq 'coding') { "Coding Plan" } else { "Agent Plan" }
    $Key = Read-Host -AsSecureString "请输入 $planName 的 API Key"
    # 将 SecureString 转为普通字符串（仅用于更新文件，不泄露）
    $KeyPtr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Key)
    try {
        $KeyPlain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($KeyPtr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($KeyPtr)
    }
} else {
    $KeyPlain = $Key
}

# 备份并更新
Backup-Env
Update-Env -TargetPlan $Target -NewKey $KeyPlain

Get-CurrentConfig

Write-Host "`n=== 下一步操作 ===" -ForegroundColor Cyan
Write-Host "1. 重启 OMP 使配置生效"
Write-Host "2. 若遇 401 错误，请确认："
Write-Host "   - Key 是否对应所选 Plan（Coding Key 用于 Coding Plan，Agent Key 用于 Agent Plan）"
Write-Host "   - Base URL 是否正确（无多余空格或斜杠）"
Write-Host "   - 模型是否在所选 Plan 中实际开通"