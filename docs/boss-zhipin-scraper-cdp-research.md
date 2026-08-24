# boss-zhipin-scraper 工作原理调研与借鉴清单

> 调研对象：`D:\mashibing\boss_spider\boss-zhipin-scraper`（v2.2）
> 调研目的：为 JobAgent 的 XHS CDP 回退后端与 Boss CDP 适配器（plan.md §8.6）提供经过实战验证的模式。
> 调研日期：2026-08-22

## 一、核心工作原理

```
┌─ 生命周期 ─────────────────────────────────────────────┐
│ --setup-chrome: 隔离 profile 启动 Chrome (9222 端口)    │
│   ~/.boss-zhipin-scraper/chrome-profile（持久登录态）    │
│   用户手动登录一次 -> 重启机器后登录态仍然保留             │
└────────────────────────────────────────────────────────┘
          ↓
┌─ 抓取循环（每页）───────────────────────────────────────┐
│ 第1页: Page.navigate 真实搜索页                          │
│   -> 被动旁听页面自己发的 joblist API 响应（零注入请求）    │
│   -> 首个响应同时完成登录/风控判定（不单独发探测请求）      │
│ 第2页起: human_scroll 滚到底 -> 触发页面自身无限滚动加载    │
│   -> 继续旁听响应 -> hasMore=false 时优雅终止              │
│ 每页: 立即落盘 + job_id 去重（异常退出不丢数据）          │
└────────────────────────────────────────────────────────┘
          ↓
┌─ 详情页 ────────────────────────────────────────────────┐
│ build_detail_url: 列表的 securityId/lid 透传进详情 URL   │
│ 只从含「职位描述」的区块提取 JD；                          │
│ 见到「登录查看完整内容」-> 明确报错停止，不保存截断正文      │
└────────────────────────────────────────────────────────┘
```

关键源码位置：

| 机制 | 位置 |
|---|---|
| 裸 WebSocket CDP 客户端（事件缓冲） | `CDPSession`（`boss_cdp_raw.py:287`） |
| 被动捕获器 | `NetworkJoblistCapture`（`boss_cdp_raw.py:444`） |
| #53 注入 XHR 被风控识别的教训注释 | `boss_cdp_raw.py:437-442` |
| 风控响应分类（两级判定） | `classify_login_probe_response`（`boss_cdp_raw.py:975`） |
| 后台标签页 + 焦点仿真 | `create_page_session` / `BACKGROUND_VISIBILITY_SCRIPT`（`boss_cdp_raw.py:398-443`） |
| 首个真实请求兼任登录判定 | `gate_first_response`（`boss_cdp_raw.py:1527`） |
| 拟人滚动触发无限滚动 | `human_scroll`（`boss_cdp_raw.py:1491`） |
| 详情上下文参数透传 | `build_detail_url`（`boss_cdp_raw.py:1378`） |
| 详情登录墙检测 | `DETAIL_LOGIN_MARKER = "登录查看完整内容"`（`boss_cdp_raw.py:624`） |
| 全局请求预算 | `incr_request` / `MAX_API_REQUESTS=500`（`boss_cdp_raw.py:65,274`） |
| 按 user-data-dir 精准关闭 Chrome | `stop_cdp_chrome` / `chrome_pids_for_user_data_dir`（`boss_cdp_raw.py:2261,2302`） |
| 隔离 profile 准备 / 登录态复制 | `prepare_cdp_profile`（`boss_cdp_raw.py:2136`） |

## 二、值得借鉴的机制（按价值排序）

### A 级：直接改变架构认知

**1. 被动捕获 > 注入请求（#53 血泪教训）**

> 程序注入的同步 XHR 与页面自身请求特征不同，会被 BOSS 风控识别为异常环境（code 37）。
> 改为导航真实搜索页 + 滚动加载，仅旁听页面自己发出的响应，全程零注入请求。

`NetworkJoblistCapture` 的实现要点：`send()` 等命令响应期间到达的 Network 事件自动入缓冲
（不丢失）；`wait_next_response(trigger=导航)` 先触发再等待。

**JobAgent 应用**：XHS 的 CDP 后端应旁听页面自己发的 `user_posted` / `feed` API 响应
（snake_case、自带新鲜 xsec_token、与 Spider_XHS 响应完全同构），
而不是"页内 fetch"——那条路已被实测淘汰。

**2. 风控分类的两级判定**

已知风控码（BOSS 的 31/37）+ 未知码按 message 关键字兜底
（"环境存在异常 / 访问频繁 / 安全校验 / 滑块"）。
关键收益：**不把「已登录但被风控」误判为登录失败**，避免提示用户重复登录、密集重试火上浇油；
一旦确认限制状态立即停止探测。

**JobAgent 应用**：`XhsCdpBlockedError` 的分类吸收这套两级判定
（`success=false` 的 msg 关键字兜底），强化"确认风控即停"语义。

**3. `--stop-chrome` 按 user-data-dir 精准匹配进程**

关闭时遍历进程命令行、比对 `--user-data-dir` 参数，**绝不按端口或进程名 kill**——
不会误伤正在使用的主 Chrome（Gmail/GitHub 登录态）。先 SIGTERM 温和终止，5 秒后仍存活才 force。

**JobAgent 应用**：CDP Chrome 启动器应提供同样安全语义的 `stop_cdp_chrome(profile_dir)`。

### B 级：工程细节值得照抄

**4. 隔离持久 profile 的安全策略**
默认不软链、不复制主 Chrome 数据；`--copy-login-state` 显式 opt-in 且只复制 Cookie 数据库
文件（不碰密码库/历史/扩展）；`--reset-chrome-profile` 显式清空。
JobAgent 的 CDP 模式比它更安全（根本不读 Cookie 文件），应保持这个优势。

**5. 首个真实请求兼任登录探测**
不发固定探测请求，登录/风控判定直接用第一次真实业务请求的响应完成。
JobAgent 应用：XHS 的第一个 `user_posted` 响应同时承担登录判定和数据返回。

**6. 后台标签页 + 焦点仿真**
自动流程创建的标签页默认不抢前台焦点；但后台页 `document.hidden=true` 会阻止页面渲染/发请求，
配套 `Emulation.setFocusEmulationEnabled` + `document.hidden` 覆盖脚本。
JobAgent 应用：Playwright `context.new_page()` 天然不抢焦点，但 XHS 页面在后台标签里
是否正常发 API 请求需真机验证。

**7. 全局请求预算（单次 500 硬顶 + 80% 预警）**
与限流（速率）正交的总量维度：无论速率多慢，单次运行请求数到顶就停。
JobAgent 应用：可作为 `XhsCdpBackend` 的会话级导航预算，防 agent 失控循环。

**8. 数据完整性纪律**
- 列表 API 数据 > DOM 数据（DOM 薪资被字体反爬污染），DOM 兜底默认禁用；
- 详情页"登录查看完整内容"= 截断，**报错而不是保存半截数据**；
- 每页立即增量落盘 + 去重。

JobAgent 应用：CDP 读取优先级应为「旁听 API 响应 > `__INITIAL_STATE__` 页面状态 > DOM」。

### 不建议照搬的部分

| 机制 | 不照搬的原因 |
|---|---|
| `human_scroll` 随机拟人化节奏、`mouse_jitter` | 属行为/指纹规避范畴，JobAgent 的 CDP 边界明确禁止 fingerprint evasion；且 XHS 作者页首屏 30 条（limit≤30）够用，无需滚动翻页 |
| 裸 WebSocket CDP 客户端（自写 `CDPSession`） | 那是为了零依赖；JobAgent 已依赖 Playwright，`connect_over_cdp` + `page.on("response")` 可等价实现被动捕获 |
| `--copy-login-state` | JobAgent CDP 模式不读 Cookie，无此需求 |

## 三、落到 JobAgent 的行动清单

| # | 借鉴项 | 目标模块 | 状态 |
|---|---|---|---|
| 1 | 被动捕获 user_posted/feed 响应 | `jobagent/scraper/xhs_cdp.py` | 待实现 |
| 2 | `stop_cdp_chrome`（user-data-dir 匹配关闭） | `jobagent/auth/cdp_chrome.py` | 待实现 |
| 3 | 风控 msg 关键字兜底分类 | `jobagent/scraper/xhs_cdp.py` | 待实现 |
| 4 | 首个响应兼任登录判定 | `jobagent/scraper/xhs_cdp.py` | 待实现 |
| 5 | 后台标签焦点问题 | 真机验证项 | 待验证 |
| 6 | 会话级导航预算 | `jobagent/scraper/xhs_cdp.py` | 可选增强 |
| 7 | API > 页面状态 > DOM 优先级 | `jobagent/scraper/xhs_cdp.py` | 待实现 |
| 8 | CDP 仅在用户配置开启时由单一组装点接入（不与 Spider_XHS 混用） | `jobagent/agent.py` 组装点 | 本次落地 |

架构约定（本次落地）：`xhs_backend.py` 保持纯 Spider_XHS 适配器，永远不知道 CDP 存在；
`xhs_cdp.py` 是独立的可选层（单向依赖 xhs_backend）；三个业务 Tool 的默认后端回归
`SpiderXhsBackend`；只有 `build_job_agent` 组装点在 `XHS_CDP_ENABLED=true` 时才懒加载
CDP 组合并注册 `ensure_xhs_cdp_chrome` 工具。CDP 关闭时零 CDP 代码加载。
