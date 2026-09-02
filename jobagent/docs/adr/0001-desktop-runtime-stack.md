# ADR-0001：采用 pywebview + PyInstaller 的桌面运行路线

- 状态：已采纳
- 日期：2026-08-31
- 范围：JobAgent 前端、桌面发布与本地运行时

## 决策

JobAgent 采用以下统一技术路线：

```text
React/Vite 前端
        ↓
FastAPI + Uvicorn API/SSE 与静态资源服务
        ↓
pywebview 原生桌面窗口
        ↓
PyInstaller onedir 桌面打包
        ↓
SQLite 保存 Journey、TaskRun、Artifact
```

前端同时支持两种运行方式：

1. 浏览器访问 FastAPI 提供的网页版界面。
2. 由 pywebview 加载同一份前端，发布为 Windows 和 macOS 桌面应用。

## 备选方案备案

### Tauri

备案为可替代的桌面外壳方案，组合形式为：

```text
React/Vite + Tauri + Python sidecar + SQLite
```

本阶段不采用。只有在包体积、内存占用、原生系统集成或安全模型成为明确瓶颈时，再重新评估。

### Electron

不作为当前方案。它生态成熟，但对本项目而言会引入额外的 Node 运行时和更大的桌面包；现有 Python Agent 与 pywebview 的集成路径更直接。

### Nuitka

可以作为 PyInstaller 的后续替代或性能/保护性打包实验，但不与 PyInstaller 同时作为正式发布链路。当前正式构建以 PyInstaller `onedir` 为准。

## 原因

- CareerDesk 已验证 React/Vite、FastAPI、pywebview、PyInstaller 的组合。
- JobAgent 的核心运行时是 Python Agent，桌面层不需要引入第二套原生语言工具链。
- 网页版与桌面版复用同一份 React 产物和 API 合约。
- `Journey` 数据天然适合本地 SQLite 持久化，桌面包可离线运行。
- `onedir` 比 `onefile` 更容易诊断、升级和携带模型/Skill/资源文件。

## 影响

- 后端需要提供 FastAPI API，以及用于 Agent 流式输出的 SSE 接口。
- pywebview 启动时需要启动本地 FastAPI/Uvicorn 服务，并将窗口指向本地地址。
- 发布构建需要分别在 Windows 和 macOS 原生环境执行 PyInstaller。
- 前端不能依赖开发期 Vite server；生产环境必须使用构建后的静态资源。
- 外部投递、发送消息等操作仍需保留明确的人机确认，不因桌面打包而绕过 HITL。
