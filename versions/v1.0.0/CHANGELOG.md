# 面试猴 · 更新日志（CHANGELOG）

> 版本号遵循语义化版本：`主版本.次版本.修订号`（major.minor.patch）
> 每次发版用 `python release.py --version x.y.z --note "..."` 自动记录，同时快照源码到 `versions/` 目录。

## [未发布]

### 进行中
- 系统音频回环采集（会议中对方的声音）
- 说话人区分（diarization）
- 接入可选 LLM：段落式纪要 + 跨句追问
- 打包成 Windows 安装包（electron-builder）

---

## [1.0.0] - 2026-11-08

### 新增
- Electron 主进程 + preload + 渲染端三层架构（`src/main.js`、`src/preload.js`、`renderer/index.html`）
- 实时录音转文字（Web Speech API）
- 实时提取：高频关键词、数字/金额/时间、已确认结论、待办事项
- 疑问解答：本地关键词检索返回相关片段
- 自动生成会议纪要，可复制 / 下载 txt
- 会议记录持久化到本地 `userData/meetings.json`

### 基线说明
- 首个可运行版本，作为版本管理基线。
- 后续 Python 侧修复（Ollama 引擎对接、404 修复、语音识别诊断脚本 diag_sr 系列）将随 1.0.1+ 版本正式记录。
