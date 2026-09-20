# 启动 mitmdump 抓 Boss 简历弹窗流量（写入 .ua/resume_flow_capture.jsonl）
# 用法: .\scripts\start_mitmdump_resume.ps1 [-Port 8080]   Ctrl+C 停止
# 配套: Chrome 需带 --proxy-server=http://127.0.0.1:8080 启动才会走此代理
# 操作: 打开任一 HR 聊天 -> 点「发简历」弹窗（G1 即到手）；可选真实发送一次在线简历（G2/G4）
param(
    [int]$Port = 8080,
    [string]$ListenHost = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$addon = Join-Path $repo 'scripts\mitm_capture_resume_flow.py'
if (-not (Test-Path $addon)) { throw "addon not found: $addon" }

$mitmdump = 'D:\Program Files\mitmproxy\bin\mitmdump.exe'
if (-not (Test-Path $mitmdump)) { $mitmdump = 'mitmdump' }  # fallback to PATH

Write-Host "mitmdump -> ${ListenHost}:${Port}  addon: $addon"
Write-Host "capture -> .ua\resume_flow_capture.jsonl"
& $mitmdump --listen-host $ListenHost --listen-port $Port -s $addon
