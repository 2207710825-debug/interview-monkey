# 面试猴 · 更新日志（CHANGELOG）

> 版本号遵循语义化版本：`主版本.次版本.修订号`（major.minor.patch）
> 每次发版用 `python release.py --version x.y.z --note "..."` 自动记录，同时快照源码到 `versions/` 目录。

## [1.2.1] - 2026-11-28

### 修复 —— 导入 API Key 后实时问答仍显示 Ollama / 连接失败
- 根因：桌面主进程在导入 `realtime_server` 时就创建了实时 FastAPI app；设置页保存 DeepSeek Key 后，旧服务仍持有启动时的 `config.yaml` Ollama provider，直到重启应用才生效
- 设置保存后通过 `BackgroundRealtimeServer.reload_llm_config()` 原地热切换 provider，无需重启；正在运行的采集管线共用同一个 service 对象，也会立即使用新模型
- 实时服务增加 config.json / config.yaml 指纹监测，在每次 HTTP 请求前自动重载；直接改配置文件也能生效
- 明确区分“配置不完整”和“连接失败”：选择 DeepSeek 但没保存 Key 时不再静默退回 Ollama，状态栏显示 `⚠ 配置不完整`，回答时提示去设置页填写 Key
- LLM 网络错误现在包含实际请求的 endpoint 和模型名（不含 Key），避免再把云端配置问题误认为本地 Ollama 连接失败
- 设置页保存后通过 iframe `postMessage` 立即刷新实时页模型标签，不再显示“下次打开自动生效”
- 新增 `tests/test_app_llm_config.py` 覆盖 provider 选择、缺 Key、热切换和可操作错误

### 新增 —— Windows 敏感窗口捕获保护
- pywebview 主窗口在 `before_show` 阶段设置 Display Affinity，并在恢复、跨屏、句柄重建后重申
- 每 2 秒回查一次实际 affinity；属性被清除时自动修复，失败信息写入 `app.log`
- 早期 Electron 入口同步启用 `BrowserWindow.setContentProtection(true)`

### 新增 —— 启动失败不再「双击没反应」
- 新增 `check_webview2()`：启动前用 WebView2Loader 的 C API 探测运行时（不依赖 .NET 包装层），拿不到就弹原生消息框给出下载地址
- `main()` 捕获 `webview.start()` 异常并弹框说明，避免窗口一闪而过
- 新增 15 秒 watchdog：`webview.start()` 既不抛错、窗口也不出现时，明确提示卡在哪一步、给三条可执行建议
- `tools/wv2_env_check.py`、`tools/wv2_version_check.py`：独立诊断脚本，用来区分「运行时坏了」还是「pywebview 包装层不兼容」

### 排查记录 —— 窗口起不来的真因
实测环境：Windows 11 Build 26200 + pywebview 6.2.1 + WebView2 Runtime 157.0.4341.42。

现象：`webview.start()` 不报错，窗口不出现，`evaluate_js` 抛 `Main window failed to start`；
.NET 侧报 `Couldn't find a compatible Webview2 Runtime (0x80070002)`。

定位结论：
- 同一台机器用**原生 COM API** 调 `CreateCoreWebView2EnvironmentWithOptions` **成功**（`errorCode=0`）
- `WebView2Loader.dll` 的 `GetAvailableCoreWebView2BrowserVersionString` 能正确返回 `157.0.4341.42`
- 把 `Microsoft.Web.WebView2.Core.dll` 从 1.0.3856.49 升到 1.0.4314.35 后错误完全不变

→ 运行时健康，故障在 pywebview 的 .NET 包装层，与本项目代码、配置持久化、流式输出均无关。

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
