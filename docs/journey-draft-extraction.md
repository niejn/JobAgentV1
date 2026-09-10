# Journey 摘要提取

摘要由 `jobagent/journey/draft.py` 统一生成。目前采用后端确定性规则，没有调用 LLM；岗位匹配评估仍是独立步骤。

- 截图上传到 `POST /api/journey-drafts/ocr`：Tesseract 提取文本后直接调用摘要服务，返回 `text`、OCR 元数据和 `draft`。
- 粘贴文本调用 `POST /api/journey-drafts/extract`，请求为 `{text, current: null}`。
- 补充或修改字段也调用 extract，传入当前 `current` 草稿；仅更新明确标注的字段，保留原始 JD 和其他字段。
- 两个接口统一返回 `draft`、`missing_fields`、`message` 和 `method: rules`。前端只提交、展示和编辑，不提取字段。

字段规则：显式标签优先；标题在职责正文之前，支持“工程师”和“开发”等后缀；公司可从文末 HR 署名识别；工作地点“临港”规范为“上海临港”；没有明确依据的部门保持空值。不同版式或严重 OCR 错字仍可能需要用户确认。

验证：项目根目录运行 `.venv/Scripts/python.exe -m pytest tests/test_journey_draft_extraction.py -q`；前端目录运行 `node --test draft-regression.test.mjs` 和 `npm run build`。OCR 接口测试替换图像识别引擎的输出，验证的是 OCR 后的服务链路，不是 Tesseract 准确率。

部署时须更新后端并刷新前端；如果后端未开启自动重载，需要重启。已缓存的旧草稿不会自动覆盖，重新上传截图或粘贴 JD 可重新提取。
