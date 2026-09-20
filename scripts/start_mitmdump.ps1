# 启动 mitmdump 抓 Boss geekEnter 请求（捕获体写入 .ua/geek_enter_capture.json）
# 用法: .\scripts\start_mitmdump.ps1 [-Port 8080]   Ctrl+C 停止
# 配套: Chrome 需带 --proxy-server=http://127.0.0.1:8080 启动才会走此代理
param(
    [int]$Port = 8080,
    [string]$ListenHost = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$addon = Join-Path $repo 'scripts\mitm_capture_geek_enter.py'
if (-not (Test-Path $addon)) { throw "addon not found: $addon" }

$mitmdump = 'D:\Program Files\mitmproxy\bin\mitmdump.exe'
if (-not (Test-Path $mitmdump)) { $mitmdump = 'mitmdump' }  # fallback to PATH

Write-Host "mitmdump -> ${ListenHost}:${Port}  addon: $addon"
& $mitmdump --listen-host $ListenHost --listen-port $Port -s $addon
