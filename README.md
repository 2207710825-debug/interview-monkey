# 面试猴 · 本地优先的会议纪要 / 实时问答 / 面试陪练

> **使用边界**：本项目只用于个人学习、会议记录和面试**前**练习。正式面试、考试、测评、背调或任何需要独立完成的场景中请勿实时使用。完整条款见 [DISCLAIMER.md](DISCLAIMER.md)。

面试猴是一个 Windows 桌面应用：本机采集会议声音或麦克风，本地完成语音识别、知识库检索和实时问答；同时提供面试陪练、练习题库和历史复盘。默认数据全部留在本机，云端只在你明确选择在线大模型时接收**问题文本和检索上下文**，不上传音频。

技术栈：**pywebview + WebView2 + Python/FastAPI + sherpa-onnx + 本地 RAG + SQLite/JSONL**。

---

## 功能

- **实时语音转写**：WASAPI 系统回环 / 麦克风采集，WebRTC VAD 分段，sherpa-onnx 流式识别 + 第二遍 SenseVoice 校准；支持中英混合技术词热词。
- **实时问答工作台**：问题自动识别、Q&A 时间线、上下文侧栏、来源引用、转写纠错、会话历史、手动提问、停止生成/重新回答。
- **本地知识库**：个人笔记、牛客题、微信资料、XMind 思维导图、PDF 资料均可导入；BM25 + 向量混合检索，只在本机建立索引。
- **练习题库**：内置题库 + 本地导入题库；支持语音或文本作答、评分、参考答案、练习记录。
- **会议纪要**：关键词、数字/金额/时间、结论、待办自动提取，可复制/下载。
- **桌面体验**：无边框深色玻璃界面、可调整背景不透明度、右下角缩放、窗口置顶、全局快捷键、摄像头手势控制。
- **本地优先**：音频、转写、索引和配置都在本机；可接本地 Ollama，也可显式选择 DeepSeek / 智谱等在线模型。

---

## 快速开始（直接使用）

1. 打开 [Releases](https://github.com/2207710825-debug/interview-monkey/releases/latest)，下载 `InterviewMonkey-v1.2.0.exe`（或对应最新版本）。
2. 系统要求：Windows 10/11 x64，已安装 **Microsoft Edge WebView2 Runtime**（Win11 通常自带）。
3. 双击运行。首次使用在「模型设置」中填写 DeepSeek Key，或启动本地 Ollama 后选择 Ollama；不填 Key 也能使用本地规则兜底和练习题库。
4. 需要实时问答时，在「智能问答」页选择音源（系统声音 / 麦克风），点击开始监听。

exe 已包含应用代码、两套本地 ASR 模型和界面资源；**不包含**你本机的知识库、题库、会议记录和 API Key。

本机数据目录：

```text
%APPDATA%\InterviewMonkey\
├─ config.json           # 模型/检索设置（API Key 只存本机）
├─ knowledge\            # notes.jsonl、nowcoder.jsonl、wechat_notes.jsonl、study_notes.jsonl…
├─ qa.jsonl              # 实时问答历史
├─ transcripts.jsonl     # 转写历史
└─ app.log               # 诊断日志
```

删除 `config.json` 等于恢复模型设置；删除 `knowledge` 下的导入文件即可移除对应知识库/题库。

---

## 知识库与题库导入

导入完全在本机执行，数据不会上传，也不在仓库或 exe 中。

```bash
# 牛客专项练习导出文件 → 知识库 + 题库
python scripts/import_nowcoder.py --raw-dir "E:\面试猴\import\nowcoder_raw"

# 微信 Markdown 笔记 → 知识库 + 题库
python scripts/import_wechat_notes.py \
  --unet "D:\...\深度学习与UNet学习笔记.md" \
  --qa "D:\...\面试高频题_答题骨架_深度学习基础_修订版.md"

# XMind + 两份 PDF → 知识库 + 题库
python scripts/import_study_pack.py \
  --xmind "E:\download\面试涉及的知识点.xmind" \
  --pdf1 "E:\download\01 神经网络(带目录-水印).pdf" \
  --pdf2 "E:\download\02 DeekLearning面试问题_精简版.pdf"
```

输出文件统一写到 `%APPDATA%\InterviewMonkey\knowledge`，应用启动时自动加载：

- `notes.jsonl`：个人笔记索引
- `nowcoder.jsonl` / `nowcoder_bank.json`：牛客题
- `wechat_notes.jsonl` / `wechat_bank.json`：微信 Markdown 资料
- `study_notes.jsonl` / `study_bank.json`：XMind / PDF 学习资料
- `*_import.json`：导入清单

导入脚本只读取你提供的文件，不下载在线题库，也不会把题目内容写入项目仓库。

---

## 架构

```text
renderer/index.html (pywebview/WebView2)
        │ js_api + HTTP + SSE
        ▼
127.0.0.1:<随机端口> FastAPI（只监听回环）
        │
        ├─ WASAPI loopback / 麦克风 → WebRTC VAD → sherpa-onnx / SenseVoice
        ├─ 问题检测 → 术语表 → BM25 + 向量 RRF → 来源片段
        ├─ 本地 Ollama / 在线 LLM（仅文本）→ SSE token 流
        ├─ qa.jsonl / transcripts.jsonl / SQLite 历史
        └─ 练习题库：内置 + 本地导入 + 语音/文本作答评分
```

关键目录：

```text
面试猴-app/
├─ 面试猴.py                 # 桌面入口、窗口、js_api
├─ realtime_server.py        # 独立/内嵌实时服务入口
├─ engine/
│  ├─ realtime/              # 音频采集、VAD/ASR、RAG、编排、历史
│  ├─ desktop_backdrop.py    # 窗口背后桌面采样（玻璃背景）
│  ├─ window_security.py     # 窗口捕获保护
│  ├─ store.py               # SQLite 本地存储
│  └─ asr_models*/           # 本地 ASR 模型
├─ renderer/                 # 主界面、玻璃主题、手势控制
├─ web/realtime.html         # 实时问答工作台
├─ scripts/                  # 本地资料导入脚本
├─ tests/                    # pytest 回归
└─ 面试猴.spec               # PyInstaller 单文件打包配置
```

---

## 源码运行

需要 Python 3.12（当前打包环境），Windows 10/11 x64。

```bash
git lfs install
git clone https://github.com/2207710825-debug/interview-monkey.git
cd interview-monkey

python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-pack.txt
.venv\Scripts\python.exe -m pip install -r requirements-realtime.txt

.venv\Scripts\python.exe 面试猴.py
```

开发时也可以单独启动实时服务：

```bash
.venv\Scripts\python.exe realtime_server.py
# 或使用 fixtures，不调用真实 ASR/RAG/LLM
.venv\Scripts\python.exe realtime_server.py --mock
```

模型由 Git LFS 管理，克隆后务必执行 `git lfs pull`；没有模型时离线 ASR 无法启动。

---

## 测试

```bash
.venv-pack\Scripts\python.exe -m pytest tests -q
```

当前回归结果：**178 passed，177 subtests passed**。覆盖实时历史/SSE API、知识库加载、题库合并、术语抽取、检索、导入解析和窗口控制。

---

## 打包 Windows 单文件版

```powershell
D:\ruanjian\python\python.exe -m venv --clear .venv-pack
.\.venv-pack\Scripts\python.exe -m pip install -r requirements-pack.txt
.\.venv-pack\Scripts\python.exe -m pip install -r requirements-realtime.txt
.\.venv-pack\Scripts\pyinstaller.exe --noconfirm --clean `
  --distpath dist-realtime --workpath build-realtime 面试猴.spec
```

产物为 `dist-realtime\面试猴.exe`。当前版本 exe 约 450 MB，包含本地 ASR 模型；GitHub 仓库只保存源码，exe 通过 Release 资产分发，不进入 Git 历史。

---

## 隐私与安全说明

- 音频采集、VAD、本地 ASR、知识库检索和练习记录默认都在本机完成。
- 选择在线 LLM 时，发送的是**问题文本、检索上下文和必要提示词**；音频不会上传。API Key 只保存在 `%APPDATA%\InterviewMonkey\config.json`。
- 玻璃背景采样只在本进程内存和页面 DOM 中使用，不落盘、不上传；它利用窗口捕获保护采样窗口**背后**的桌面。
- 窗口捕获保护（`WDA_EXCLUDEFROMCAPTURE`）用于避免会议窗口被普通截图/录屏误采集，不是 DRM，也不能阻止相机拍摄、内核级采集或恶意进程；不得用它规避监考、录屏审计或单位规定。
- 导入的第三方题库/PDF 只保存在本机，使用者应自行确认拥有相应使用权，不得再分发。

---

## 免责声明

本项目是个人学习与效率工具，**不提供也不鼓励**任何形式的面试作弊、考试作弊、测评规避、监考规避、保密信息窃取或违反雇主/客户规定的行为。正式面试、考试和需要独立完成的评估中请勿实时使用。

使用者应自行确保录音/转写已取得必要同意，并遵守当地法律、会议参与者约定、平台条款和单位制度。AI 输出可能错误或过时，重要决策必须人工核实。项目按“现状”提供，不附带任何明示或暗示担保；作者不对使用本项目造成的损失、处罚、数据泄露或第三方索赔承担责任。

完整条款请阅读 [DISCLAIMER.md](DISCLAIMER.md)。

---

## 第三方与致谢

本项目使用或参考了 pywebview、WebView2、sherpa-onnx、FastAPI、Ollama、MediaPipe、DeepSeek/智谱等第三方组件或服务；相关名称和商标归各自所有者。项目与牛客网、微信、DeepSeek、智谱、Ollama、Google/MediaPipe 等均无隶属或背书关系。第三方资料的版权归原作者所有，本仓库不附带第三方题库原始数据。

---

## 版本与更新

- `CHANGELOG.md`：版本变更记录
- `versions/`：历史源码快照
- GitHub Releases：Windows 单文件 exe 与版本说明
