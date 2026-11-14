# 面试猴 · 更新日志（CHANGELOG）

> 版本号遵循语义化版本：`主版本.次版本.修订号`（major.minor.patch）
> 每次发版用 `python release.py --version x.y.z --note "..."` 自动记录，同时快照源码到 `versions/` 目录。

## [未发布]

_(暂无)_

---

## [1.1.0] - 2026-11-10

**一句话：启动不用重填 Key，开口问话 5ms 见字。**

### 新增 —— 配置持久化（痛点 1）
- 新增 `engine/config_store.py`：JSON 配置持久化，原子写入（临时文件 + `os.replace`），断电/崩溃不会写出半个文件
- schema 版本号 + 默认值合并，老配置升上来不丢字段；损坏配置静默回落默认值
- 保存语义改为**合并式 patch**：`save({"top_k": 5})` 只动检索条数，其余原样保留
- 界面只拿脱敏视图：`has_key` / `key_masked`（`sk-a****wxyz`），API Key 明文永不下发前端
- `save_config` 收到空 Key 时**沿用已存 Key**，解决「改个检索条数就把 Key 抹掉」
- 新增 `bootstrap()`：启动即探测 Ollama（`/api/tags`）与云端 Key，返回一句人话就绪说明
- 新增 `probe_backends()` / `auto_pick_provider()`：`auto` 模式下本地 Ollama 可用就自动切过去
- 界面：Key 输入框用 placeholder 显示「已保存：sk-a****wxyz」，留空 = 沿用；新增 Base URL、检索条数、重新检测按钮、启动就绪提示条

### 新增 —— 流式问答（痛点 2）
- 新增 `OllamaBackend.generate_stream()`：NDJSON 逐行解析，`done:true` 收尾
- 新增 `OpenAICompatBackend.generate_stream()`：SSE `data:` 逐帧解析，`[DONE]` 收尾；兼容部分网关返回整包 `message` 的情况
- 新增 `RuleBasedBackend.generate_stream()`：规则兜底也按行吐，界面表现一致
- 新增 `Assistant.ask_stream()`：检索 → 流式推理，`on_chunk` 回调逐段吐字，返回值仍是完整答案
- 三级降级链路：主后端 → 本地 Ollama → 规则兜底，任一环节失败自动往下走，用户只看到一句提示
- `[SKIP]` 流式判定：缓冲前 6 字符再判断是否为跳过标记，标记本身一次都不会闪到界面上
- 主进程新增 `ask_stream(text, recent)`：后台线程跑推理，用 `evaluate_js` 把增量反向推给渲染端（绕开 js_api 请求-响应模型），每轮带自增 `stream_id`，过期流自动丢弃
- Prompt 改为关键词先行：首行 3 个关键词（≤15 字），空行后再展开 3 个要点
- 渲染端：首行关键词 19px 加粗高亮，正文带闪烁光标；`requestAnimationFrame` 合帧，避免逐字刷 DOM
- Base URL 可在界面覆盖，接自建/中转网关不用改代码

### 新增 —— VAD 防误触（痛点 2.4）
- `VAD_SILENCE` 环境变量可调断句静音阈值，默认 0.8s
- 前端四道门槛：①去标点后 <4 字的口头语不问 ②作答中新问题排队不打断 ③同一句 1.2s 内重复定稿只认一次 ④排队句与上一句相同则丢弃

### 新增 —— 测试
- `test_config_persist.py`：7 个场景（首次启动 / 保存 / 模拟重启 / 脱敏 / 只改条数不丢 Key / 老配置兼容 / 损坏兜底）
- `test_stream_e2e.py`：假 Ollama 服务验证首字延迟、逐字回调、内容一致、SKIP 不泄漏、降级兜底
- 真实环境冒烟：主程序导入、配置跨实例持久化、三 Provider 后端构建、自定义 Base URL、无 Key 规则兜底，11 项全通过

### 实测（`test_stream_e2e.py`，假 Ollama 每字 60ms）
| 指标 | 全量等待 | 流式 |
|---|---|---|
| 首字可见 | 3.944s | **0.005s** |
| 回调次数 | 1 | 65 |
| 内容一致性 | ✓ | ✓ |
| SKIP 标记泄漏 | — | 0 次 |

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
