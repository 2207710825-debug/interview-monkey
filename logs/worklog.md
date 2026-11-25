# 工作记录

## 2026-11-14 (UTC)

### Phase 0 基线审计（此前实际运行结果）

| 命令/操作 | 结论 | 涉及范围 |
|---|---|---|
| `\.venv-pack\Scripts\python.exe -m pytest -q tests` | 通过：125 passed，177 subtests passed，25.91s | `tests/` |
| `python -m pytest -q` | 失败：pytest 收集根目录 `test_app_api.py` 时执行导入期问答并 `sys.exit(0)`，pytest 报 `INTERNALERROR` / `no tests ran` | 根目录测试脚本 |
| `python test_config_persist.py` | 通过 | `engine/config_store.py` 持久化回归 |
| `python test_stream_e2e.py` | 失败：`OllamaBackend` 缺少 `generate_stream` | `engine/llm.py`、`test_stream_e2e.py` |
| 启动 `\.venv-pack\Scripts\python.exe realtime_server.py`，检查 `GET /api/status`、`GET /api/knowledge`、`POST /api/answer/stream` | 服务绑定 `127.0.0.1:18765`；HTTP 200；SSE 顺序为 `meta`、150 个 `token`、`done`。记录到 RAG 11.257ms、TTFT 556.083ms、端到端 556.086ms | `realtime_server.py`、`engine/realtime/api/app.py` |
| 运行前检查 `test_fix_404.py`、`test_ollama_e2e.py` | 未运行在线凭据调用；后者读取用户配置。避免把在线测试或用户凭据带入基线记录 | 根目录测试脚本 |

### 本次 GitHub 版本管理准备

| UTC 时间 | 命令/操作 | 结论 | 文件 |
|---|---|---|---|
| 2026-11-14 06:58 | `gh auth status`、`gh repo view 2207710825-debug/interview-monkey` | 已认证为 `2207710825-debug`；目标仓库此前不存在，拟创建私有仓库 | GitHub 远端 |
| 2026-11-14 06:58 | `git init -b main`、`git lfs install --local`、设置仓库本地 Git 身份 | 初始化成功；Git LFS 可用；配置 GitHub noreply 邮箱 | `.git/` |
| 2026-11-14 06:59 | 检查大文件、录音和凭据形状字符串 | exe/build/dist 和本地录音不纳入；ONNX 由 LFS 管理。扫描到的凭据形状值位于测试夹具：3 个 `sk-test` 合成值和 1 个本次替换的虚构 Zhipu 格式样例，没有发现待上传的真实 Key | `.gitignore`、`.gitattributes`、`test_config_persist.py`、`test_fix_404.py` |
| 2026-11-14 06:59 | 修改配置输出与在线测试默认行为 | `test_app_api.py`、`test_ollama_e2e.py` 输出前将 `api_key` 替换为 `[REDACTED]`；`test_fix_404.py` 默认只测格式识别，联网测试须显式设置 `MH_RUN_ONLINE_TESTS=1` 与 `MH_ZHIPU_TEST_API_KEY` | `test_app_api.py`、`test_ollama_e2e.py`、`test_fix_404.py` |
| 2026-11-14 07:00 | 新增仓库忽略规则与 LFS 克隆说明 | 忽略 exe、备份、build/dist、venv、缓存、用户状态、录音和运行日志；跟踪 `*.onnx`；README 增加 LFS 克隆步骤 | `.gitignore`、`.gitattributes`、`README.md` |
| 2026-11-14 07:00 | `\.venv-pack\Scripts\python.exe -m py_compile test_app_api.py test_ollama_e2e.py test_fix_404.py` | 语法检查通过；暂存审查为 277 个文件，0 个 exe/备份/pkg，LFS 8 个唯一 ONNX 对象（历史版本路径复用相同对象） | 3 个脱敏测试脚本、Git 暂存区 |
| 2026-11-14 07:18 | `git push -u origin main`；`git ls-remote --heads origin`；`gh api repos/2207710825-debug/interview-monkey/branches` | LFS 的 8 个对象（470 MB）上传完成；Git 引用推送因 `github.com:443` 连接中断/超时而失败，远端当时没有分支。`curl` 到 `github.com` 超时，但 GitHub API 可用 | GitHub 远端、`.git/` |
| 2026-11-14 07:18 | `gh api --method PUT repos/2207710825-debug/interview-monkey/contents/README.md --input -` | 空仓库 Git Data API 返回 409 后，用内容 API 建立 README 种子提交 `6c0fb3922318cd8bbd27d4e79e35b6ff0f8d1428`；仓库确认是私有，默认分支为 `main` | GitHub 远端 `main` |
| 2026-11-14 07:23 | GitHub Git Data API 创建唯一 blob、tree、commit 和 refs | 246 个唯一 blob 均返回与本地相同 SHA；277 个文件树 SHA 相同；远端 commit 与本地 `aa821c31cad2e47082fe12c6fc77368789b0605f` 完全一致。私有仓库 `main` 与 `feat/protocol-contract-mock` 均已存在；远端 API 可读到 LFS 模型原始大小 239,233,841 字节 | GitHub 远端、`.git/` |
| 2026-11-14 07:24 | `git rev-parse HEAD^{tree}` | PowerShell 将花括号表达式解析成了错误参数；提交本身成功，后续用 `git show -s --format=%T HEAD` 读取 tree SHA | 本地 `.git/` |
| 2026-11-14 07:27 | 根据 GitHub API 返回的分支 SHA 执行 `git update-ref`、`git branch --set-upstream-to`、`git switch -c feat/protocol-contract-mock` | 本地 `main` 与功能分支均跟踪对应远端分支；当前工作区干净。因 Git HTTPS 连接不可用，远端跟踪引用根据已核验 API SHA 设置 | 本地 `.git/` |

## Phase 1：协议契约与 Schema（2026-11-14 UTC）

| UTC 时间 | 命令/操作 | 结论 | 文件 |
|---|---|---|---|
| 2026-11-14 07:50 | 读取 `/api/*` 路由、`AudioFrame`、SSE service、出站 ASR WebSocket provider 与前端 SSE handler | 以实际 legacy API 为依据；未改服务端路由。本次新增 v1 是计划契约，明确等 Phase 2 实现 | `engine/realtime/api/app.py`、`engine/realtime/contracts.py`、`engine/realtime/llm/service.py`、`engine/realtime/asr/providers.py`、`web/realtime.html` |
| 2026-11-14 07:51 | `\.venv-pack\Scripts\python.exe -m pip install -r contract\requirements.txt` | 安装 `jsonschema 4.26.0` 及依赖；contract requirements 同时列出 live 检查使用的 `httpx` | `contract/requirements.txt`（虚拟环境在 `.venv-pack/`，已忽略） |
| 2026-11-14 07:52 | `\.venv-pack\Scripts\python.exe -m py_compile contract\validate_contract.py`；JSON 标准库解析 `contract/**/*.json` | Python 语法通过，24 个 JSON 文件可解析 | `contract/` |
| 2026-11-14 07:52 | `\.venv-pack\Scripts\python.exe contract\validate_contract.py` | 19 个示例通过 5 个 Draft 2020-12 Schema；音频示例的解码长度、36 字节头和 CRC32 语义校验通过 | `contract/schemas/`、`contract/examples/` |
| 2026-11-14 07:53 | 启动 `\.venv-pack\Scripts\python.exe realtime_server.py`；运行 `\.venv-pack\Scripts\python.exe contract\validate_contract.py --live`；停止服务 | 绑定 `127.0.0.1:18765`；`POST /api/answer/stream` 返回 200；288 个 SSE 事件全部符合 legacy Schema（meta=1、token=286、done=1）；未打印回答正文 | `contract/validate_contract.py`、现有 realtime server |

### 安全边界与后续状态

- 仓库计划设为私有。提交前复核暂存区；确认没有 exe、venv、用户录音或真实凭据后才推送。
- `samples/loop*.wav` 是本地 loopback 录音，因此忽略；`samples/tts*.wav` 与 `eval/asr/samples/` 为合成评测样例，可纳入。
- 本阶段只建立 GitHub 远端、基线提交和功能分支；Phase 1 协议实现等待用户确认 Phase 0 差距分析。

## Phase 2/3：Mock 协议、联调测试与交付（2026-11-14 UTC）

| UTC 时间 | 命令/操作 | 结论 | 文件 |
|---|---|---|---|
| 2026-11-14 08:27 | 新增 `engine/realtime/protocol.py`，实现 36 字节小端音频头、CRC32、payload 长度、序号/重复帧与时间戳校验 | 仅在协议入口增加网络帧验证，不改原 `AudioFrame` | `engine/realtime/protocol.py` |
| 2026-11-14 08:28 | 新增 `engine/realtime/mock_backend.py` 与 `fixtures/realtime_mock.json` | Mock 回答和转写只读 fixtures；增加 DEV_TOKEN、loopback 检查、600 秒内存恢复、ping/pong、JSONL 脱敏协议日志和故障注入 | `engine/realtime/mock_backend.py`、`fixtures/realtime_mock.json` |
| 2026-11-14 08:29 | `create_app(..., mock=True)` 增量接入并实现 `realtime_server.py --mock`；常规 `realtime_server.py` 保持默认分支 | `/v1/realtime` 只在 Mock app 注册；现有 SSE API 与前端事件字段保留 | `engine/realtime/api/app.py`、`realtime_server.py` |
| 2026-11-14 08:30 | 对实际 Uvicorn WebSocket 握手拒绝进行联调 | 当前服务器在不支持 denial response 时对缺失/无效令牌都返回 HTTP 403；契约记录相同状态码及 JSONL 内部原因分类 | `contract/API_CONTRACT.md`、`engine/realtime/mock_backend.py` |
| 2026-11-14 08:31 | 首轮 `pytest -q tests/test_mock_protocol.py` | 暴露异步连接包装、握手拒绝差异、故障重复触发、脚本导入路径和 delay case EOF 触发问题；逐项修正，没有移除测试 | `tests/test_mock_protocol.py`、`tests/sim_client.py`、`engine/realtime/mock_backend.py` |
| 2026-11-14 08:32 | `pytest -q tests/test_mock_protocol.py` | 协议、CRC/seq、控制、恢复、鉴权、心跳、延迟、故障注入和 50 session 测试通过；最终全量输出见 `deliverables/logs/pytest-final.txt` | `tests/test_mock_protocol.py` |
| 2026-11-14 08:33 | 启动 `realtime_server.py --mock`，在浏览器打开 `127.0.0.1:18765` 并点击“开始采集” | 可访问性状态为 capturing/connected；三栏显示 fixture 转写、当前问题、知识来源和分段答案 | `web/realtime.html`（只读验证）、Mock 服务 |
| 2026-11-14 08:34 | `tests/frontend_alignment.py --base-url http://127.0.0.1:18765` | 输出 `ALIGNMENT_PASS`；SSE 事件为 status、transcript、question_decision、answer(meta/token/token/token/done)；结果写入交付日志 | `tests/frontend_alignment.py`、`deliverables/logs/frontend_mock_alignment.jsonl` |
| 2026-11-14 08:35 | `tests/sim_client.py --url ws://127.0.0.1:18765/v1/realtime --concurrency 50 --duration 30` | 50/50 session；15,000 个 PCM 帧；48,000,000 字节；ACK p50 28.139 ms、p95 67.321 ms；总计 49.468 秒；各会话统计隔离通过 | `tests/sim_client.py`、`deliverables/logs/sim50x30.txt` |
| 2026-11-14 08:36 | 从 `deliverables/server` 启动服务快照，并通过 `tests/sim_client.py` 建立 1 路闭环 | 可单独启动；1 帧、3200 字节、done 完成 | `deliverables/server/` |
| 2026-11-14 08:37 | `\.venv-pack\Scripts\python.exe contract\validate_contract.py` | `EXAMPLES_PASS count=19 schemas=5` | `contract/`、`deliverables/logs/contract-validation.txt` |
| 2026-11-14 08:38 | `\.venv-pack\Scripts\python.exe -m pytest -q tests` | 最终 `136 passed, 68 warnings, 177 subtests passed in 17.11s`；旧 125 条测试未回归；弃用警告来自 websockets/Uvicorn 适配层 | `tests/`、`deliverables/logs/pytest-final.txt` |
| 2026-11-14 08:38 | `git diff --check`、交付文件镜像、源码快照核对 | 契约、Schema、examples、所有 tests 文件、副本 server、运行日志、报告均放入 `deliverables/`；只保留 Python 源码快照，不复制模型二进制 | `deliverables/` |
| 2026-11-14 08:50 | 活动 session 二次恢复保护合入快照后，从 `deliverables/server` 启动 `realtime_server.py --mock` 并发送单帧 | 启动于 `127.0.0.1:18767`；1 会话、3200 字节，ACK p50 1.31ms，done 成功 | `deliverables/server/`、`deliverables/logs/phase3_verification.txt` |
| 2026-11-14 09:01 | `gh api` Git Data API 发布并校验 Phase 2/3 两个提交对象与功能分支引用 | 远端 `feat/protocol-contract-mock` 与本地文件树一致；默认分支 `main` 保持原 SHA | GitHub `feat/protocol-contract-mock` |
| 2026-11-14 12:33 | `node --check` 检查内嵌脚本、Python `compile()` 检查启动器；PyInstaller 构建至 `dist-shortcut` 并从 EXE 归档核对资源 | 检查通过；EXE 内含 Ctrl+Shift+M 提示、前端按钮和 `minimize_window` 调用；旧 exe 未覆盖 | `renderer/index.html`、`面试猴.py`、`dist-shortcut/面试猴.exe` |
| 2026-11-14 13:38 | `面试猴.py` 注册 Win32 全局 `Ctrl+Shift+Down/Up`，前端同步显示快捷键并保留页面焦点回退；重新生成 `dist-shortcut\面试猴.exe` | Python 与 2 段内嵌 JS 语法检查、`git diff --check` 通过；EXE 运行日志确认两个 RegisterHotKey 成功；投递 `WM_HOTKEY` 验证最小化/恢复处理器成功。`SendInput` 未触发 `WM_HOTKEY`，故真实物理按键未端到端验证 | `面试猴.py`、`renderer/index.html`、`dist-shortcut\面试猴.exe` |
| 2026-11-25 02:47 | 读取本机简签 `jianqian/ui/main_window.py` 中 `glass_alpha`（60–230、默认178）及 pywebview WebView2 透明窗口实现；修改面试猴窗口参数、页面表面 alpha 滑块与 iframe 同步 | 背景透明度控件值保存到 localStorage；透明背景仅作用于主要表面，文字和按钮不整体变淡；Python、两个页面脚本语法与 `git diff --check` 通过。本次未运行 pytest、未打包 EXE、未提交，避免带入现存 ASR/RAG 工作区改动 | `面试猴.py`、`renderer/index.html`、`web/realtime.html` |
| 2026-11-25 03:05 | 在面试陪练页增加语音/文本回答切换；文本支持按钮或 Ctrl+Enter 提交，并复用既有答题历史、统计与评分流程；修正文本框字体 CSS | `renderer/index.html` 内嵌 JavaScript 语法解析通过（1 个脚本），`git diff --check` 通过；未运行 pytest、未进行桌面 GUI 手动验证、未打包 EXE | `renderer/index.html` |
| 2026-11-25 03:39 | 在 `E:\pythondemo\gesture_canvas\web` 找到在线手势演示源码与 MediaPipe 本地资源；按用户确认的目标把握拳最小化、丢手恢复接入面试猴，复用现有 pywebview 窗口 API | ES module 语法（2 个文件）、内嵌脚本语法和 `git diff --check` 通过；7 个复制资源 SHA-256 与来源一致。未启摄像头做 GUI 实测、未运行 pytest、未重打包 EXE | `renderer/index.html`、`renderer/gesture/` |
| 2026-11-25 06:00 | 按用户要求改善摄像头共用：停止/断流/采集停帧都会释放 MediaStream；摄像头独占失败显示 Windows 多应用共享设置指引；确认本机 Windows 11 25H2 | ES module 语法（2 个文件）、内嵌脚本语法、`git diff --check` 通过；未启用系统共享开关、未做真实摄像头双应用联测、未运行 pytest、未重打包 EXE。并行使用仍依赖 Windows/相机的多应用模式支持 | `renderer/index.html`、`renderer/gesture/control.js`、`logs/worklog.md` |
| 2026-11-25 01:50 America/Chicago | 真实摄像头采集与释放测试：使用已有 OpenCV 5.0.0 + Windows MSMF，默认索引 0；本机唯一在线物理摄像头为 Chicony USB2.0 Camera | 单进程 23 帧、两个独立进程同时采集 105/10 帧、释放后重开 14 帧，均为 1280x720；所有采集成功且 release 后 isOpened=false，子进程全部退出。未录音、未保存图像。浏览器手势模块加载成功但停在权限等待，测试页已关闭；此结果不代表旧 EXE/WebView2 或腾讯会议联测通过。临时服务/文件已清理 | logs/camera-hardware-20261125.jsonl |
| 2026-11-25 06:50 America/Chicago | 将背景透明度改为所有内容页面共用：原位替换背景色，覆盖侧栏、按钮、表单、卡片、弹层；移除实时问答重复铺底，iframe 与子页采用一致 color-scheme；重打包并替换根目录 EXE | Edge 实际渲染验证 10 个模块，父/子页 alpha 均由 0.235 变为 0.902，文字 opacity=1；刷新保留 76%。内嵌脚本语法、3 个现有嵌入测试、git diff --check 通过；16 个包内资源哈希与源码一致。旧版已备份。桌面自动化启动器两次失败且文件管理器窗口均最小化，未完成新版 EXE 启动复核；Windows 原生标题栏不在页面 CSS 透明度范围内 | renderer/index.html、web/realtime.html、面试猴.exe；logs/glass-20261125-*.json/log |

## 2026-11-25 无边框深色玻璃 UI（继续 Codex 未完成项）

| UTC 时间 | 命令/操作 | 结论 | 文件 |
|---|---|---|---|
| 2026-11-25 13:32 | 将主窗口改为 `transparent=True, frameless=True, easy_drag=False`，设置 `.pywebview-drag-region` 直接目标拖拽；新增最大化/关闭 API 与顶部自绘窗口按钮 | 去掉原生标题栏和整块黑色底；独立同参数 pywebview 窗口在白底上实测桌面可透过玻璃层，文字保持不透明 | `面试猴.py`、`renderer/index.html` |
| 2026-11-25 13:32 | 新增 `theme-glass.css`：深蓝玻璃卡片、细边框、紧凑工具栏、蓝青强调色；把透明度滑块改为“背景不透明度”并将下限钉在 150/255（59%），避免旧 76% 透明度把面板和文字冲淡 | 主界面、陪练、题库、设置、AI 聚焦模式已按深色玻璃主题重排；应用级黑色背景板移除，桌面可从窗口边缘和内容底层透出 | `renderer/theme-glass.css`、`renderer/index.html` |
| 2026-11-25 13:32 | 调整陪练按钮层级、题库 chip 换行、手势按钮图标化；补充窗口控制测试 | `pytest tests`：174 passed，177 subtests passed；JS/Python 语法检查通过 | `renderer/index.html`、`renderer/gesture/control.js`、`tests/test_embedded_realtime.py` |
| 2026-11-25 13:32 | 重新打包并替换根目录 EXE，清理 Codex 留下的 dist/build/旧 EXE 备份 | 最终 `面试猴.exe` SHA256 `a0d5c685cbf29f14276e6c5a1789369ca1cbff927d4ff41d4e96da1e12ae0077`；包内 `renderer/index.html`、`theme-glass.css`、`web/realtime.html` 与源码哈希一致；启动后 `/api/status` 为 notes_chunks=2856、asr_terms=206 | `面试猴.exe` |

### 2026-11-25 13:45 复核修复与最终包

| UTC 时间 | 命令/操作 | 结论 | 文件 |
|---|---|---|---|
| 2026-11-25 13:45 | 按独立复核修复：玻璃控件旧绝对定位复位；横幅/提示改为高对比固定深色；最大化状态接入 `events.maximized/restored` 并同步页面；增加右下角拖拽缩放 API；侧栏标题区域可拖拽；提问按钮在手动记录有内容时直接加入记录 | `pytest tests`：174 passed，177 subtests passed；JS/Python 语法检查通过 | `renderer/theme-glass.css`、`renderer/index.html`、`面试猴.py`、`tests/test_embedded_realtime.py` |
| 2026-11-25 13:45 | 重新打包、启动冒烟、替换根目录 EXE 并清理所有 dist/build/旧 EXE | 最终 SHA256 `498b962e8fca9577ee5aaffe4c82acc5c99cb66fef47932e7e93711d64838d01`；启动后 `/api/status` notes_chunks=2856、asr_terms=206、DeepSeek 正常；项目内只保留一个应用 exe | `面试猴.exe` |

| 2026-11-25 13:55 | 针对用户圈出的窗口本体黑底，新增 `engine/desktop_backdrop.py`：利用窗口已有的 `WDA_EXCLUDEFROMCAPTURE`，每 0.8s 采样窗口背后的桌面并作为页面背景；只在本机内存/DOM 使用，不落盘。修正主题 CSS 只用 `background-color` 清底，避免覆盖采样图 | 独立测试：受保护窗口采样后关闭 WDA，截图中窗口边缘显示洋红桌面而非黑底；真实渲染页采样后 background-image 长度 169018；启动日志确认 `[backdrop] 桌面背景采样已启动`，未出现采样失败 | `engine/desktop_backdrop.py`、`engine/window_security.py`、`面试猴.py`、`renderer/theme-glass.css` |
| 2026-11-25 13:55 | 最终重打包、启动冒烟并替换根目录 EXE | 最终 SHA256 `d5b5a259e5f0ba5c32ee966b44f620649f5ef2228fff384a9aa8e2cb442ac346`；`/api/status` notes_chunks=2856、asr_terms=206、DeepSeek 正常；项目内只保留一个 exe | `面试猴.exe` |

## 2026-11-25 导入 XMind 与两份 PDF 学习资料

| UTC 时间 | 命令/操作 | 结论 | 文件 |
|---|---|---|---|
| 2026-11-25 14:11 | 新增 `scripts/import_study_pack.py`：解析 XMind `content.json`、PDF1 目录/页面、PDF2 字号识别的问答；写入 `study_notes.jsonl`、`study_bank.json`、`study_import.json` | XMind 434 分块/91 题；神经网络 PDF 159 分块/171 题；深度学习面试 PDF 73 分块/129 题；合计新增 666 分块、391 题 | `scripts/import_study_pack.py`、`%APPDATA%/InterviewMonkey/knowledge/study_*.json*` |
| 2026-11-25 14:12 | 加载器增加 `study_notes.jsonl`，题库合并增加 `study_bank.json`；补学习资料解析/加载测试 | 知识库总分块 3522；题库总数 1991；题库分类为 XMind 91、PDF2 129、PDF1 171；RAG 可检索 YOLOv8、深浅拷贝、C++ 指针/引用、BN 等新内容 | `engine/realtime/api/app.py`、`面试猴.py`、`tests/test_study_import.py`、loader/bank tests |
| 2026-11-25 14:14 | 全量测试、重打包、启动冒烟并替换根目录 EXE | 178 passed，177 subtests passed；启动 `/api/status` notes_chunks=3522、asr_terms=206、DeepSeek 正常；最终 SHA256 `4bd3d8c9d22c310e5de1991763b946fd50686ab7046e39da31314a065f15d48f`；项目内只保留一个 exe | `面试猴.exe` |
