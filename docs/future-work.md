# Future Work

## 已实现：损坏多模态 checkpoint session 的兼容处理

- 文本模型调用时只生成 request 级别的消息投影，不删除 checkpoint 中的原始图片。
- 模型明确返回不支持图片的 400 后，按“模型名 + Base URL”记录为 `text`，并清洗后重试一次。
- 悬空并发 tool call 补充取消结果并写回 checkpoint，不自动重放旧工具。
- `invalid_tool_calls` 和孤立 `ToolMessage` 在恢复时清理。
- 如果 checkpoint 本身无法反序列化，仍需要单独的数据库恢复工具。
