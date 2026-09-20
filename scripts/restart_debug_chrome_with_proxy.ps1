# 重启调试 Chrome 并强制走 mitmproxy（8080）
# 用法: .\scripts\restart_debug_chrome_with_proxy.ps1   （确认 mitmdump 已在跑）
$ErrorActionPreference = 'Stop'

# 关掉现有调试 Chrome（profile 保留，登录态不丢）
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  Where-Object { $_.CommandLine -match 'boss-chrome-small-profile' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList `
  '--remote-debugging-port=9222',`
  '--user-data-dir=D:\mashibing\jobclaw\data\boss-chrome-small-profile',`
  '--proxy-server=http://127.0.0.1:8080',`
  '--no-first-run','--no-default-browser-check'

Start-Sleep -Seconds 4
$proxied = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  Where-Object { $_.CommandLine -match 'proxy-server' } | Select-Object -First 1
if (-not $proxied) { Write-Host "WARN: proxy flag missing - retry or check chrome path" }
else { Write-Host "OK: debug Chrome running through 127.0.0.1:8080" }
