---
name: ChromeCDP-setup
description: "Chrome DevTools 调试模式连接管理。检测并启动本地 Chrome 的远程调试端口（默认 9222）。"
author: JobAgent
version: 1.0.0
---

# Chrome DevTools 调试模式连接管理

## 工作流程

使用 `execute` Shell 工具按步骤执行，获取预先打开的 Chrome 浏览器页面中的内容。

### 步骤 1 — 检查端口是否已就绪

执行以下 PowerShell 命令检查 `http://127.0.0.1:9222` 是否可访问：

```powershell
$resp = Invoke-WebRequest -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 2 -UseBasicParsing -ErrorAction SilentlyContinue
if ($resp.StatusCode -eq 200) { Write-Output "READY"; $targets = Invoke-WebRequest -Uri "http://127.0.0.1:9222/json" -TimeoutSec 3 -UseBasicParsing; Write-Output "Pages: $($targets.Content | ConvertFrom-Json | Measure-Object | Select -ExpandProperty Count)" } else { Write-Output "NOT_READY" }
```

- 输出包含 `READY` → 端口已就绪，可直接使用
- 输出 `NOT_READY` → 进入步骤 2

### 步骤 2 — 查找 Chrome 可执行文件

```powershell
$paths = @("$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe")
$found = $false; foreach ($p in $paths) { if (Test-Path $p) { Write-Output "FOUND: $p"; $found = $true; break } } if (-not $found) { Write-Output "NOT_FOUND" }
```

- 输出了路径 → 记录此路径
- `NOT_FOUND` → 提示用户安装 Chrome

### 步骤 3 — 启动 Chrome 调试模式

```powershell
$profileDir = "$env:USERPROFILE\.jobagent\chrome-cdp-profile"
if (-not (Test-Path $profileDir)) { New-Item -ItemType Directory -Path $profileDir -Force | Out-Null }
& "CHROME_PATH" --remote-debugging-port=9222 --user-data-dir="$profileDir" --no-first-run --no-default-browser-check "https://www.google.com"
```

> 将 `CHROME_PATH` 替换为步骤 2 找到的路径。

### 步骤 4 — 等待端口就绪

```powershell
$timeout = 20; $sw = [Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $timeout) {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:9222/json" -TimeoutSec 1 -UseBasicParsing -ErrorAction SilentlyContinue
    if ($resp.StatusCode -eq 200) {
        $pages = ($resp.Content | ConvertFrom-Json).Count
        Write-Output "READY after $([math]::Round($sw.Elapsed.TotalSeconds, 1))s, $pages pages"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
Write-Output "TIMEOUT after $([math]::Round($sw.Elapsed.TotalSeconds, 1))s"
exit 1
```

- `READY` → 调试端口 `http://127.0.0.1:9222` 可用
- `TIMEOUT` → 提示检查安全软件

## 注意事项

- 首次使用需在打开的 Chrome 窗口中手动登录目标网站
- 登录态保存在隔离 profile（`~/.jobagent/chrome-cdp-profile`），重启后保留
- Agent 通过 `execute` 工具执行 Shell 命令完成操作