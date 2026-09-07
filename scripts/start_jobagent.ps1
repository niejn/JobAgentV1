<#!
.SYNOPSIS
    Start the JobAgent backend and frontend development servers.

.EXAMPLE
    .\scripts\start_jobagent.ps1

.EXAMPLE
    .\scripts\start_jobagent.ps1 -InstallFrontend -StartChat
#>

[CmdletBinding()]
param(
    [switch]$InstallFrontend,
    [switch]$StartChat,
    [int]$BackendPort = 8008,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path
$UiRoot = Join-Path $Root "jobagent\ui"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 Python 虚拟环境：$Python。请先创建 .venv。"
}

if (-not (Test-Path -LiteralPath (Join-Path $UiRoot "package.json"))) {
    throw "未找到前端目录：$UiRoot"
}

$BackendCommand = @"
Set-Location -LiteralPath '$Root'
& '$Python' -m uvicorn jobagent.web:app --reload --port $BackendPort
"@

$FrontendCommand = if ($InstallFrontend) {
@"
Set-Location -LiteralPath '$UiRoot'
npm install
npm run dev -- --port $FrontendPort
"@
} else {
@"
Set-Location -LiteralPath '$UiRoot'
npm run dev -- --port $FrontendPort
"@
}

Start-Process -FilePath "powershell.exe" `
    -WorkingDirectory $Root `
    -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $BackendCommand)

Start-Process -FilePath "powershell.exe" `
    -WorkingDirectory $UiRoot `
    -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $FrontendCommand)

if ($StartChat) {
    $ChatCommand = @"
Set-Location -LiteralPath '$Root'
& '$Python' -m jobagent.cli chat
"@
    Start-Process -FilePath "powershell.exe" `
        -WorkingDirectory $Root `
        -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $ChatCommand)
}

Write-Host "JobAgent 后端：http://127.0.0.1:$BackendPort" -ForegroundColor Green
Write-Host "JobAgent 前端：http://localhost:$FrontendPort" -ForegroundColor Green
if ($StartChat) {
    Write-Host "JobAgent CLI Agent 已在独立窗口启动。" -ForegroundColor Green
}
