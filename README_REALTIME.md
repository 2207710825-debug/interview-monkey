# 面试猴实时语音问答核心

这是现有 pywebview 桌面项目中的独立异步闭环，不复制参考项目的目录或源码。默认使用本地 ASR、Ollama 与本地知识库；任何云端 ASR/LLM 在 `local_mode=true` 或未取得 `third_party_consent` 时都会被配置层拒绝。

## 架构与数据流

```text
WASAPI Loopback / Mic / monitor
        │ 20ms PCM16，丢最旧有界队列
        ▼
WebRTC VAD + Adaptive Segmenter ──200ms pre-roll / 300~700ms 静音 / 15s 强切
        ▼
Local Sherpa 或 JSON WebSocket ASR ──partial / final
        ▼
声道门控 → speaker 过滤 → 问题检测 → 3s 合并
        ▼                         └──新问题取消旧生成
partial 推测检索              final 查询改写
        └──────────────┬─────────────┘
                       ▼
        BM25 + 向量索引 + RRF → 引用与低置信度模板
                       ▼
        OpenAI 兼容 LLM → SSE meta/token/done/error
                       ▼
        requestAnimationFrame 合帧 + 增量 Markdown
```

每个问题生成一个 `trace_id`。`LatencyTracker` 记录 VAD、ASR 首 partial、RAG、TTFT、渲染和端到端阶段，并输出 JSON 日志；`/api/status` 返回进程内 P50/P95 直方图摘要。

## 阶段交付

1. `contracts.py`、`config.py`、`events.py`、`latency.py` 与 Mock：稳定接口、背压、隐私门控。
2. `capture/`、`vad/`、`asr/`：WASAPI/PortAudio、动态断句、本地及 WebSocket ASR、热词。
3. `qa/`：声道/speaker 过滤、问题检测、指代改写、合并与打断。
4. `rag/`：MD/PDF/TXT 摄入、语义切块、BM25 + 向量 + RRF、推测检索和上下文预算。
5. `llm/`、`api/`：OpenAI 兼容流、错误分类、FastAPI SSE。
6. `web/realtime.html`：三栏页面、状态栏、控制按钮、增量渲染。
7. `tests/test_realtime_*.py`、`eval/`：单测、离线闭环与 20 条评测集。

## 安装与运行

```powershell
cd E:\面试猴\面试猴-app
.\.venv-pack\Scripts\python.exe -m pip install -r requirements-realtime.txt
.\.venv-pack\Scripts\python.exe realtime_server.py
```

浏览器打开 `http://127.0.0.1:18765`。首次开始采集前，页面会明确显示采集状态；默认仅本地模式。麦克风只进入上下文，只有系统环回声道会触发回答。端口可用 `MH_REALTIME_PORT` 覆盖。

打包 Windows 单文件桌面版：

```powershell
.\.venv-pack\Scripts\python.exe -m pip install -r requirements-pack.txt -r requirements-realtime.txt
.\.venv-pack\Scripts\pyinstaller.exe --noconfirm --clean --distpath dist-realtime --workpath build-realtime 面试猴.spec
```

产物为 `dist-realtime\面试猴.exe`。EXE 会保留原 pywebview 主界面，并自行启动本地服务；点击左侧“智能问答”即可使用嵌入的实时工作台，不需要手动运行 `realtime_server.py` 或打开浏览器。

配置集中在 `config/config.yaml`。密钥不写入文件，只读取 `MH_LLM_API_KEY`。常用覆盖：

```powershell
$env:MH_LOCAL_MODE = 'false'
$env:MH_THIRD_PARTY_CONSENT = 'true'
$env:MH_LLM_BASE_URL = 'https://your-gateway.example/v1'
$env:MH_LLM_MODEL = 'your-model'
$env:MH_LLM_API_KEY = '...'
```

## 验证与延迟报告

```powershell
# 新闭环测试
.\.venv-pack\Scripts\python.exe -m unittest discover -s tests -p "test_realtime_*.py" -v

# 全部旧/新回归
.\.venv-pack\Scripts\python.exe -m unittest discover -s tests

# 评测器自检；这不是模型效果
.\.venv-pack\Scripts\python.exe eval\run_eval.py --mock

# Mock 编排延迟基线；不代表真实 ASR/网络/模型
.\.venv-pack\Scripts\python.exe tests\bench_realtime.py

# 真实模型评测
.\.venv-pack\Scripts\python.exe eval\run_eval.py --answers path\to\answers.jsonl
```

性能报告必须区分两类数据：

- Mock 离线测试只证明事件顺序、格式和预算埋点正确，不能代表真实 ASR/LLM 延迟。
- 真实 P50/P95 需用户主动开始采集，在目标设备、目标模型和真实网络下运行，再从结构化 `qa_latency` 日志或 `/api/status` 取样。

## 已知边界

- Windows WASAPI Loopback 是当前实机目标；macOS 需选择 ScreenCaptureKit 桥接或 BlackHole 等虚拟声卡，Linux 选择 PulseAudio/PipeWire monitor source。PortAudio 适配器已支持这些输入设备，但本轮未在非 Windows 机器实测。
- 可选 FAISS 存在时自动使用 `IndexFlatIP`；未安装时使用同接口的内存余弦索引。
- Speaker diarization 的 `speaker_id` 合同与指定提问方过滤已实现，具体 pyannote/云服务模型未硬编码，需按部署环境注入。
- 窗口安全隔离仍由现有 `engine/window_security.py` 负责，不属于本闭环的音频或文本传输权限。
