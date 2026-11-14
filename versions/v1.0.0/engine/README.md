# 面试猴 · 语音助手引擎

五段式架构实现（对应你给的图）：

```
[音频采集 audio.py] → [语音转文本 asr.py] → [知识检索 rag.py] → [大模型推理 llm.py] → [呈现 GUI]
   PCM Raw Audio        Whisper/sherpa-onnx     Vector 检索          Ollama/vLLM/API
```

## 各段与可插拔后端

| 段 | 文件 | 依赖 | 无依赖兜底 |
|---|---|---|---|
| 音频采集 | `audio.py` | `sounddevice`（可选） | 返回 None，改用文本/Web 语音 |
| 语音转文本 | `asr.py` | `sherpa-onnx` 或 `faster-whisper`（可选） | 返回 None，改用文本提问 |
| 知识检索 | `rag.py` | 无（纯 Python TF-IDF + 余弦） | 已内置，开箱即用 |
| 大模型推理 | `llm.py` | `Ollama`（可选）或 OpenAI 兼容 API | 规则化抽取式摘要 |
| 编排 | `assistant.py` | — | — |

## 快速体验（零依赖，即可运行）

```bash
python demo.py "面试猴有什么功能"
python demo.py "如何提问" --status
```

## 启用本地大模型（离线问答，推荐）

1. 安装 [Ollama](https://ollama.com)，拉一个中文模型：
   ```bash
   ollama pull qwen2.5:7b
   ```
2. 引擎会自动检测 `localhost:11434` 并使用本地模型，无需改代码。
   也可指定：`python demo.py "问题" --ollama-model qwen2.5:7b`

## 启用本地语音识别（数据不出本机）

```bash
pip install sherpa-onnx
# 下载中文流式模型，例如 sherpa-onnx-streaming-zipformer-zh-14M-*
python demo.py "问题" --asr-model <模型目录>
```

## 接入 OpenAI 兼容 API

在代码里用 `llm.build_llm(openai_base=..., openai_key=..., openai_model=...)`，
即可接入 OpenAI / DeepSeek / 本地 vLLM 等任何 OpenAI 兼容服务。

## 知识库

把 `.txt` / `.md` 文档放进 `engine/knowledge/` 即被自动索引，问答时优先引用。
