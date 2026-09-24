---
name: resume-docx-layout
description: "把简历内容 JSON 渲染成 v7 定稿版式的 docx（A4 窄边距、Calibri+微软雅黑、节标题下划线、右对齐日期制表位、居中页码页脚）。改内容不改代码。"
author: JobAgent
version: 1.0.0
---

# 简历 docx 排版（v7 版式引擎）

`scripts/build_resume_docx.py` 是排版引擎（版式常量在脚本顶部），简历内容在
`assets/resume_v7_content.json`。出新版简历 = 复制一份 content JSON 改文本，
不碰排版代码。

## 工作流程

### 步骤 1 — 复制内容文件

```
cp assets/resume_v7_content.json /workspace/resume_v15_content.json
```

只改 `blocks` 里的文本字段。结构（block 类型与顺序）保持不动，除非用户明确
要求增删板块。

### 步骤 2 — 渲染

```
.venv/Scripts/python.exe skills/resume-docx-layout/scripts/build_resume_docx.py \
  --content /workspace/resume_v15_content.json --out data/resumes/<新文件名>.docx
```

### 步骤 3 — 校验并回报

渲染成功后回报输出路径与段落数；用 `read_file` 无法读 docx，向用户报告
路径请其打开确认版式（页数、字体、日期右对齐）。

## 内容 schema

| block 类型 | 字段 | 用途 |
|---|---|---|
| `section` | text | 节标题（12pt 主色加粗 + 底部细线） |
| `body` | text | 正文段（左缩进 0.1cm） |
| `bullet` | text | "• " 字面量项目段（悬挂缩进） |
| `entry` | left=[[text,{bold,size}],…], right | 经历条目：左侧公司/职级 + 右侧日期（右对齐制表位 17.8cm） |
| `project` | text, note, before? | 项目标题 11pt 加粗 + 灰色时间段 |
| `meta` | label, text | "角色：/技术栈：" 行 |
| `label` | text | 加粗小标签（如"项目实现："） |
| `num` | num, text | "1、" 编号段（悬挂缩进） |

header：`name`（17pt 居中主色）、`contact`（9.5pt 灰色居中）、`footer`
（页脚左段 + 自动 PAGE 域页码，8pt）。

## 版式事实（实测沉淀）

- 版心：A4，边距上 1.35 / 下 1.25 / 左右 1.6 cm；正文 9.5pt，行距 1.22。
- 字体：Calibri（西文）+ 微软雅黑（CJK），每个 run 显式写
  w:ascii/w:hAnsi/w:eastAsia（`run.font.name` 不写 eastAsia，会按 Unicode
  块回退错字体）。
- 历史坑：2026-09-18 工作区终版脚本调用不存在的 `add_right_tab_stop()`，
  实际产出的 v7 docx 里**没有**制表位（日期落在默认制表位网格）；本引擎用
  `add_tab_stop(pos, WD_TAB_ALIGNMENT.RIGHT)` 修正，从此日期真正右对齐。
- 引用文本使用弯引号（""），bullet 是字面量 "• "（不用 Word 编号 XML）——
  与终版脚本一致，区别于 09-18 早期产出。

## 铁律

- 简历是外发物料：内容任何实质变更（经历、数字、时间）必须先给用户逐条
  确认，渲染只做排版。
- 输出写入 `data/resumes/`，不得覆盖 Boss 平台已引用的既有文件名；新版本
  用新文件名。
- 本 skill 只做本地文档生成，不触碰任何平台发送通道。
