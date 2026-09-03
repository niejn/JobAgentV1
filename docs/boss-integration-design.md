# JobAgent Boss 直聘接入设计

> 状态：设计决策已确认，尚未实施 `BossCdpAdapter`。最后更新：2026-08-22。

## 1. 决策

保留 JobAgent 现有业务 Tool、领域模型、状态管理和安全策略，借鉴
`D:/mashibing/boss_spider/boss-zhipin-scraper` 的 CDP 被动采集核心，实现新的
`BossCdpAdapter`，并将其作为 Boss 只读岗位发现的默认 Adapter。

不直接依赖或运行 `boss-zhipin-scraper` CLI/Hermes Skill，不整体复制其 2620 行单文件；
`mcp-bosszp` 只作为历史接口线索，不作为运行依赖。MCP 是未来面向外部客户端的 Adapter，
不能成为 JobAgent 内部业务调用的必经层。

## 2. 目标与非目标

### 目标

- JobAgent 根据 Resume/Candidate Background、Job Search Profile 和当前 Search Strategy
  Portfolio，自主生成少量 Boss 查询并取得真实 Job Posting。
- 支持关键词、城市、商圈后过滤、公司规模、融资阶段、薪资、经验、学历和行业。
- 保留明文薪资、稳定岗位 ID、公司特征、招聘者活跃状态、`securityId`、`lid` 和完整 JD。
- 复用真实 Chrome 会话发出的页面请求，降低独立 HTTP Client 与浏览器环境不一致的风险。
- 遇到登录墙、验证码、`code=31/37` 或其他限制状态时停止，不绕过、不自动切换 Adapter 重试。
- 为未来岗位去重、周报、定制简历、HITL 投递和结果追踪提供统一领域数据。

### 非目标

- 不破解验证码、不伪造设备指纹、不轮换代理或对抗平台风控。
- 不把“CDP”描述为无法检测；它只是复用真实浏览器环境，仍属于自动化访问。
- 不直接复用 scraper 的 CSV、通用摘要和提示词；JobAgent 已有自己的 Artifact 与分析链路。
- 不在岗位发现 Tool 中打招呼、发送简历或投递。
- 不把 Cookie、CDP WebSocket URL、`securityId` 或 `lid` 返回给 LLM。

## 3. 调研结论

### 3.1 JobAgent 现状

JobAgent 已具备：

- `discover_boss_jobs` LangChain Tool 和 `BossDiscovery` seam。
- 统一 `Job` 模型、Candidate Profile、Opportunity Journey 与本地 Artifact。
- Cookie Provider 自动匹配、`~/.jobclaw -> ~/.jobagent` 兼容迁移、过期检查和 Playwright
  Cookie 规范化。
- `BossHttpBackend` 的关键词/城市/公司规模搜索、串行限频、风控冷却和结构化 blocked 结果。
- `BossHttpBackend` 已落地为 `httpx` 只读 Adapter，可通过 `BOSS_SEARCH_TRANSPORT=http` 显式启用；
  默认仍使用 CDP。真实账号直连可能返回环境异常，遇到风控必须停止而不是重试或切换指纹。
- 已安装 Playwright Chromium；真实 `jobagent login --platform boss --check` 通过。

当前不足：

- `BossHttpBackend` 使用独立 `curl_cffi` 会话，真实调用出现过 `code=37`，不宜作为默认来源。
- 旧 `BossScraper` 以 DOM 卡片为中心，缺少成熟的无限滚动 API 捕获与严格详情 JD 校验。
- 列表摘要不能代替完整 JD，不能据此生成最终匹配结论或定制简历。

### 3.2 `mcp-bosszp`

可借鉴内容：推荐流端点、部分参数映射、`securityId/encryptBossId/lid` 字段和二维码登录状态机。

不采用原因：

- 当前主文件存在语法错误，不能直接启动。
- Jobs Resource 仍返回硬编码示例。
- 真实 Tool 调用个性化推荐流，不支持关键词、城市、商圈和公司规模等核心需求。
- 全局 Session/线程状态不可安全支持多个会话。
- 登录信息 Resource/Tool 向模型返回完整 Cookie 和 BST。
- 固定 security-check seed、timestamp、AES 输入和 User-Agent 已过时且脆弱。
- 打招呼 Tool 没有 HITL、幂等、限额、冷却、资格判断和成功确认。
- 没有测试。

结论：只作为接口研究材料，不作为 library、MCP Server 或子进程依赖。

### 3.3 `boss-zhipin-scraper`

值得借鉴：

- `CDPSession` 与 `NetworkJoblistCapture`：监听页面自身的
  `/wapi/zpgeek/search/joblist.json` 响应并读取 response body。
- 真实搜索页导航、无限滚动、焦点仿真和后台 target 管理。
- API 岗位字段映射、城市码表和筛选代码。
- `code=31/37`、登录墙、空结果和响应错误的分类。
- 详情页 JD 提取、登录截断检测、招聘者卡片/页面噪声剔除。
- 随机低频节奏、请求预算、增量原子写入与真机 smoke 流程。

不直接依赖的原因：

- 核心 2620 行单文件同时承担 CDP、Chrome 进程、CLI、文件、CSV、详情和分析职责。
- 接口以同步 CLI 和文件输出为中心，不适合 JobAgent 的异步 Tool 与 Journey Artifact。
- 使用全局请求计数和默认 `~/.boss-zhipin-scraper` 目录，不符合 JobAgent 配置与状态边界。
- 商圈只能从结果 location 后过滤，CLI 没有稳定的业务级 `area` interface。
- Windows 仅完成单测和基础 CLI 验证，真实列表/详情链路仍需在本机重新验收。
- README 部分章节仍描述旧的注入 XHR 方式，与当前“被动 Network 捕获”实现不完全一致。

## 4. 目标模块与 seam

```text
JobAgent conversation / future Supervisor
  -> discover_boss_jobs Tool                 LangChain Adapter
     -> BossJobDiscovery                     application module
        -> BossGateway                       stable seam
           ├── BossCdpAdapter                default, read-only
           ├── BossHttpAdapter               experimental, disabled by default
           └── FakeBossAdapter               tests

Future external client
  -> FastMCP Adapter
     -> same BossJobDiscovery / BossGateway

Application workflow (separate high-impact seam)
  -> prepare_boss_application                draft only
  -> HITL approve/edit/reject
  -> BossApplicationGateway                  external write
```

### 4.1 `BossGateway` interface

概念接口保持小而深：

```python
class BossGateway(Protocol):
    async def health(self) -> BossSourceHealth: ...
    async def discover(self, request: BossDiscoveryRequest) -> BossDiscoveryResult: ...
    async def get_job_detail(self, reference: BossJobReference) -> BossJobDetail: ...
```

Interface 隐藏：CDP 端口、target/session ID、页面选择器、滚动次数、API path、Cookie、
`securityId`、`lid`、输出目录和 Chrome 进程参数。Tool 参数保持用户语义，不让 LLM 操作底层传输。

`discover` 返回来源摘要与稳定 reference；只有 `get_job_detail` 通过完整性校验后，岗位才能进入
最终匹配、简历定制或投递准备。

### 4.2 读写分离

岗位发现和投递必须是两个 seam。`BossCdpAdapter` 第一阶段只读；未来
`BossApplicationGateway` 才允许外部写操作，并且必须满足：

- 已有具体 Opportunity Journey 和完整 JD。
- 已生成并确认 Tailored Resume 与招呼语。
- Application Eligibility 已通过。
- 用户对本次对象和内容明确 approve。
- 平台响应和会话状态双重确认成功后才记录 APPLIED。
- 每日限额、同公司多岗位规则、幂等记录和风控冷却生效。

## 5. Chrome 与数据目录

默认使用 JobAgent 专用、持久化的有界面 Chrome Profile：

```text
~/.jobagent/boss/chrome-profile
~/.jobagent/boss/runtime
```

- 不默认连接日常主 Chrome，避免 CDP 暴露 Gmail、GitHub 等无关会话。
- 首次使用在专用 Chrome 中人工登录 Boss；后续复用该 Profile。
- 如用户明确授权，可一次性导入主 Chrome 的 Local State/Cookie 数据，但不复制密码、历史和扩展。
- CDP 只监听 `127.0.0.1`；不向局域网开放调试端口。
- Job Posting、JD 和分析报告进入 JobAgent Artifact/SQLite，不写
  `~/.boss-zhipin-scraper/job-result`。

## 6. `BossCdpAdapter` 实现策略

从参考项目小范围重写/移植以下算法，并保留 MIT NOTICE：

1. CDP WebSocket 请求/响应关联与事件缓冲。
2. 安全创建后台 target、可见性/焦点处理。
3. `Network.enable` + joblist response 被动捕获。
4. API Job Posting 字段规范化。
5. 首次响应登录/风控/空结果判定。
6. 有界无限滚动及 `hasMore` 停止。
7. 详情 target、JD 提取和完整性校验。
8. 城市静态码表与在线更新的确定性回退。

不移植 CLI、CSV、摘要、默认目录、全局状态和任意进程终止逻辑。Chrome 生命周期、限频、
Artifact 和日志由 JobAgent 管理。

不要自动从 `BossCdpAdapter` 降级到 `BossHttpAdapter`：一个 Adapter 命中风控后切换传输重试会
增加风险。降级必须是用户可见的诊断决策，并遵守同一冷却窗口。

## 7. Tool 与 MCP 决策

第一阶段使用内部 LangChain Tool，因为 JobAgent 是唯一调用方，额外 MCP Server 只会增加状态、
错误和部署层次。Tool 与 MCP 都调用同一 application module：

```text
LangChain Tool ─┐
                ├─> BossJobDiscovery -> BossGateway
FastMCP Tool  ──┘
```

只有出现外部调用方（Claude Desktop、其他 Agent 或独立服务）时才实现 FastMCP Adapter。MCP
不得拥有独立 Cookie、Chrome、限频或 Application 状态。

## 8. 分阶段实施

### BCDP-1：只读单页 tracer bullet

- JobAgent 配置 CDP 地址与 `~/.jobagent/boss/chrome-profile`。
- `health()` 区分 CDP 不可用、未登录、风控和可用。
- 单关键词、单城市、单规模搜索，捕获一页真实 API 响应并映射 Job Posting。
- 验证上海、五角场后过滤、0–20/20–99 人和明文薪资。

### BCDP-2：分页、去重和持久化

- 有界无限滚动、`hasMore`、请求预算和取消。
- 使用 `encryptJobId` 作为 Boss 外部 ID，跨周去重。
- 保存 Recommendation Run、Posting 版本和来源时间。

### BCDP-3：完整 JD

- 按 `securityId/lid/encryptJobId` 打开详情页。
- 登录墙、导航壳、过短 JD 和页面噪声必须拒绝。
- 完整 JD 通过校验后才能执行匹配分析和 Tailored Resume。

### BCDP-4：默认切换

- `BossCdpAdapter` 成为默认；`BossHttpAdapter` 默认关闭。
- Windows 真机依次通过 `health -> smoke -> 1 页 -> 1 条详情 -> 2 页滚动`。
- 更新状态板、README、`.env.example` 和故障诊断说明。

### BCDP-5：投递与 MCP（后续）

- 先完成 BossApplicationGateway、HITL、幂等和成功确认。
- 确有外部调用方后，再添加薄 FastMCP Adapter。

## 9. 验收标准

- Agent 用户只需表达岗位、城市、商圈、公司/职位特征，无需操作 CDP 参数。
- 同一运行每轮最多一个 Boss 搜索 Tool；翻页和详情由 module 内部有界执行。
- 返回岗位包含真实明文薪资、公司规模、稳定外部 ID、来源 URL 和完整 JD 状态。
- `code=31/37`、登录墙或验证码产生结构化 blocked 结果，不重试、不崩溃、不伪造数据。
- 不向 LLM、日志、Artifact 或 MCP 返回 Cookie/CDP 凭证。
- Chrome/Profile/结果目录全部位于 JobAgent 命名空间。
- 单元测试、Ruff、mypy 通过，并完成 Windows 真机 smoke 验证。

## 10. 成本判断

- 继续从零完善 Playwright DOM：需要重新实现被动响应捕获、无限滚动、详情完整性和焦点问题，
  成本中高。
- 直接执行/import scraper：原型快，但长期被 CLI、全局状态、文件输出和独立目录绑定，维护成本高。
- **推荐方案**：现有 JobAgent seam + 借鉴 CDP 核心。预计只读搜索/详情为中等工作量，但风险和
  重复造轮子最少，后续 Tool/MCP/多 Agent 都可复用同一 module。

