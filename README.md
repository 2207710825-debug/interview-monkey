# 面试猴 · 全语音零门槛随身助手

## Git 克隆与模型文件

本仓库使用 Git LFS 管理 sherpa-onnx 模型文件。克隆后先安装 Git LFS，再拉取模型对象：

```bash
git lfs install
git clone https://github.com/2207710825-debug/interview-monkey.git
cd interview-monkey
git lfs pull
```

没有 Git LFS 时，模型路径会只有指针文本，离线 ASR 无法启动。

全语音 · 零门槛 · 实时转写 + 流式问答，配置一次就不用再填，数据只存本地。

> 实际技术栈：**pywebview + WebView2 + Python 后端**（早期 1.0.0 为 Electron 版，1.1 起迁移，原因见下方第二节）。

## 一、先逆向 → 再正向（本项目的由来）

本项目通过对「面试狗」的 REA 逆向分析，摸清了它的三层架构，然后正向复用了这套**通用技术骨架**，实现功能正当的「面试猴」：

| 逆向学到的技术 | 面试猴中的正向复用 |
|---|---|
| 主进程负责窗口 + IPC | pywebview 主窗口 + `js_api` 暴露受控方法 |
| preload 用 contextBridge 暴露受控 API | `Api` 类只暴露白名单方法，渲染端拿不到文件系统 |
| 渲染端单页（原生 JS，零依赖） | `renderer/index.html` 单页 |
| 音频采集 → 实时转写 → 分析管线 | getUserMedia 采 PCM → sherpa-onnx 本地识别 → 规则/LLM 分析 |
| 本地持久化（面试狗用 SQLite） | 配置存 JSON（原子写入）、会话存本地文件 |

## 二、目录结构

```
面试猴-app/
├─ 面试猴.py                # 入口：窗口 + Api（js_api）+ 流式推送
├─ engine/
│  ├─ config_store.py      # 配置持久化（原子写入 / schema 版本 / 脱敏）
│  ├─ llm.py               # 三种后端 + 流式（Ollama NDJSON / OpenAI SSE / 规则）
│  ├─ assistant.py         # 编排：检索 → Prompt → 流式推理 → 三级降级
│  ├─ asr.py               # sherpa-onnx 本地语音识别（VAD 断句 0.8s 可调）
│  └─ retriever.py         # 本地知识库检索
├─ renderer/index.html     # 界面：渲染 + VAD 门槛 + 流式打字机
├─ test_config_persist.py  # 配置持久化 7 场景回归
└─ test_stream_e2e.py      # 流式端到端回归（含假 Ollama 服务）
```

## 三、两个核心体验的设计

### 3.1 配置一次，以后都不用填

```
启动 → ConfigStore.load() → 合并默认值 → bootstrap() 探测
                                            ├─ 有 Key → 直接可用
                                            ├─ 检测到 Ollama 且选 auto → 自动切本地
                                            └─ 都没有 → 界面提示"去填 Key 或启动 Ollama"
```

- **原子写入**：先写同目录临时文件再 `os.replace`，中途断电也不会留下半个 JSON
- **合并式保存**：`save_config` 只传要改的字段，空 `api_key` 表示"沿用旧的"，改检索条数不会把 Key 抹掉
- **Key 明文不出后端**：前端只看到 `has_key: true` + `key_masked: "sk-a****wxyz"`，输入框 hint 就是这个脱敏串
- **向前兼容**：配置带 `version` 字段，缺字段一律补默认值，老配置文件可直接用

### 3.2 张嘴就有字（流式）

```
语音定稿 → 四道 VAD 门槛 → aiDispatch → Api.ask_stream
                                            ↓ 后台线程
                         检索 → Prompt → 后端流式生成
                                            ↓ evaluate_js 反向推送
        __onChunk(sid, piece) × N → requestAnimationFrame 合帧 → 首行关键词高亮
                                            ↓
                     __onDone / __onSkip / __onError → aiBusy=false → 发排队那句
```

- `js_api` 是请求-响应模型，等结果就是全量等待。这里改用 `window.evaluate_js()` 从 Python 线程反向推增量，渲染端不等返回
- 每轮一个自增 `stream_id`，回调里对不上就丢弃，实现"新的来了旧的自动作废"
- Prompt 强制首行 3 个关键词（≤15 字），一出现就用 19px 加粗高亮，视觉上"立刻有反应"
- 三级降级：主后端失败 → 本地 Ollama → 规则兜底，链路上任何一环挂掉用户都能拿到话
- `[SKIP]` 判定缓冲前 6 字符，标记本身不会闪到界面上

## 四、功能

- 🎙 实时录音转文字（本地 sherpa-onnx，离线可用；浏览器 Web Speech 兜底）
- ⚡ 流式问答，首字约 5ms（本地假服务实测，见 CHANGELOG）
- 🔑 实时提取：高频关键词、数字/金额/时间、已确认结论、待办事项
- 📋 自动生成纪要，可复制 / 下载
- 💾 配置与会话全部本地存储，不联网也不丢
- 🛡 Windows 窗口捕获保护：启动前设置 Display Affinity，恢复、跨屏和句柄重建后自动巡检重申

> 窗口捕获保护覆盖 Windows 支持的公共截图/录屏路径，但不是 DRM，不能阻止相机拍摄、
> 内核级采集或进程内恶意代码。运行日志中的 `[DLP]` 记录包含实际 HWND、回查值和错误码。

## 五、运行

### 5.1 装依赖

```bash
pip install pywebview            # 窗口壳（Windows 上自动用 WebView2）
pip install sherpa-onnx sounddevice numpy   # 语音识别 + 麦克风采集
```

> 只在还没装过时执行。`pywebview` 是必需依赖，缺了会直接 `ModuleNotFoundError: No module named 'pywebview'`。

### 5.2 启动

```bash
cd 面试猴-app
python 面试猴.py
```

首次运行：点右上「模型设置」，填 DeepSeek Key（或启动本地 Ollama 后直接选 ollama）→ 保存 → 下次打开已就绪。

配置文件位置：`%APPDATA%\InterviewMonkey\config.json`（界面里的「模型设置 → 重新检测」下方会显示实际路径）。删掉它就等于恢复出厂设置。

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | 本地 Ollama 地址 |
| `VAD_SILENCE` | `0.8` | 断句静音阈值（秒），越小越灵敏、也越容易被环境噪音切碎 |

### 5.3 打包 Windows 单文件版

项目使用隔离虚拟环境和锁定依赖打包，避免把系统 Python 中的 PyTorch、PyQt 等无关组件带入 EXE：

```powershell
D:\ruanjian\python\python.exe -m venv --clear .venv-pack
.\.venv-pack\Scripts\python.exe -m pip install -r requirements-pack.txt
.\.venv-pack\Scripts\python.exe -m pip install -r requirements-realtime.txt
.\.venv-pack\Scripts\pyinstaller.exe --noconfirm --clean --distpath dist-realtime --workpath build-realtime 面试猴.spec
```

构建结果位于 `dist-realtime\面试猴.exe`。冻结后的程序仍以原 `renderer/index.html` 作为主界面，并在“智能问答”模块内嵌三栏实时工作台；后台 FastAPI 只监听随机回环端口。`面试猴.spec` 会携带原界面、配置、提示词、实时网页、知识库、两套 ASR 模型及所需本机运行库。

跑回归：

```bash
python test_config_persist.py    # 配置持久化 7 场景
python test_stream_e2e.py        # 流式端到端（自带假 Ollama）
```

### 5.4 窗口起不来怎么查

桌面应用最怕「双击没反应」。程序启动前会自检 WebView2 运行时，起不来时弹框说明；如果连弹框都没有，按下面两步定位。

```bash
python tools/wv2_version_check.py   # 系统里认到哪个 WebView2 版本
python tools/wv2_env_check.py       # 用原生 COM 创建环境，成功=运行时没问题
```

两个脚本的判读方式：

- `wv2_version_check.py` 打印出版本号（如 `157.0.4341.42`）→ 运行时**已装且健康**
- `wv2_env_check.py` 打印 `>>> 原生 API 创建环境成功` → 运行时**完全正常**

如果上面两项都通过、但窗口仍然起不来，那就是 **pywebview 的 .NET 包装层**与当前 Windows 版本不兼容，不是本项目代码的问题。已在 Windows 11 Build 26200 + pywebview 6.2.1 上复现过：`webview.start()` 不抛错、窗口不出现，.NET 侧报 `Couldn't find a compatible Webview2 Runtime (0x80070002)`，而同一台机器用原生 COM API 创建环境却成功（换 SDK 到 1.0.4314.35 也无效）。

这种情况下依次尝试：

1. 「设置 → 应用」里修复 `Microsoft Edge WebView2 Runtime`
2. 装 Windows 最新累积更新
3. 删掉 `%APPDATA%\InterviewMonkey\EBWebView\Default\Cache`（只清页面缓存，不动 `Local Storage`，历史记录不受影响）
4. 换一个 Windows 账户或降级到 pywebview 5.4 试

> 顺带一个容易踩的坑：WebView2 会缓存 `http://127.0.0.1:<port>/index.html`。改了 `renderer/index.html` 却发现界面没变，多半是缓存还在。清掉上面第 3 条的 `Cache`、`Code Cache`、`GPUCache` 三个目录即可（**不要删 `Local Storage`**，那是你的记录）。

## 六、版本管理

`CHANGELOG.md` 记录每次更新，`versions/` 保存源码快照，`release.py` 一键发版。

```bash
python release.py --show                                  # 查看当前版本
python release.py --version 1.1.0 --note "流式 + 配置持久化"  # 发布新版本
```

## 七、Roadmap

- [x] 配置持久化 + 启动自动检测（1.1.0）
- [x] 流式输出 + 关键词先行 + 三级降级（1.1.0）
- [x] VAD 断句阈值可调 + 前端四道门槛（1.1.0）
- [ ] 系统音频回环采集（会议中对方的声音）
- [ ] 说话人区分（diarization）
- [ ] 打包成 Windows 安装包

## 八、实时语音问答核心

异步音频 → VAD → 流式 ASR → 问题检测 → 混合 RAG → LLM SSE → 三栏前端的独立运行说明见 [`README_REALTIME.md`](README_REALTIME.md)。该链路默认仅本地模式，云端传输必须显式确认。
