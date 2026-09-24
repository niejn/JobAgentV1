---
name: chrome-cdp-setup
description: "Chrome DevTools 调试模式连接管理。检测并启动本地 Chrome 的远程调试端口（默认 9222）。"
author: JobAgent
version: 2.0.0
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

### 步骤 3 — 完全退出 Chrome，复制日常 profile 到专用目录

⚠️ **两个硬约束（2026-08-27 真机验证）**：

1. **Chrome 136+ 在默认 profile 上静默忽略 `--remote-debugging-port`**——必须用
   显式 `--user-data-dir` 指向非默认目录。
2. **Boss 直聘的 warlock 设备指纹风控（`warlockdata.min.js`）会拦截全新 profile**：
   页面加载正常但前端主动跳 `about:blank`，joblist API 永不发出。带真实浏览
   历史/登录态/指纹的日常 profile 副本则放行。

因此正确做法是**复制日常 profile**（而非新建空目录），两者兼得：

```powershell
# 3a. 完全退出 Chrome（复制时有文件锁会失败）
Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3

# 3b. 复制日常 profile（排除缓存类目录，1.7G 源约复制 10-30s / 900MB）
robocopy "$env:LOCALAPPDATA\Google\Chrome\User Data" "$env:USERPROFILE\.jobagent\chrome-daily-copy" /E /XD "Cache" "Code Cache" "GPUCache" "GrShaderCache" "ShaderCache" "Crashpad" "Component CRX" /XF "LOCK" /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { Write-Output "COPY_FAILED"; exit 1 }
```

- 日常 profile 里的登录态、浏览历史、设备指纹数据全部随副本生效（cookie 加密
  key 在 `Local State`，同一 Windows 用户 DPAPI 下可解密）。
- 副本目录可长期复用；日常 profile 更新后（新登录/新历史）想同步再复制一次。

### 步骤 4 — 启动副本 profile 的调试模式

```powershell
Start-Process -FilePath "CHROME_PATH" -ArgumentList '--remote-debugging-port=9222','--user-data-dir=C:\Users\YOU\.jobagent\chrome-daily-copy','--no-first-run','--no-default-browser-check','https://www.zhipin.com/'
```

> 将 `CHROME_PATH` 替换为步骤 2 找到的路径，`YOU` 替换为用户名。

### 步骤 5 — 等待端口就绪

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

- **keeper tab**：调试 Chrome 只剩一个 tab 且该 tab 被站点反爬关闭/重定向时，
  最后一个窗口消失会连带整个 Chrome 退出。批量自动化任务前确认有至少一个
  非目标站点的 tab 存活（或由 `BossApplier`/`BossCdpBackend` 补一个）。
- Boss 登录态判定用 CDP cookies（`wt2`/`bst`/`wbg` 任一存在），不要依赖 DOM
  探测——Boss 前端会把页面跳到 `about:blank` 使 DOM 查询失败。
- 登录态保存在副本 profile（`~/.jobagent/chrome-daily-copy`），重启后保留；
  失效时重走步骤 3 重新复制（日常 Chrome 里先登录）。
- Agent 通过 `execute` 工具执行 Shell 命令完成操作
