# -*- coding: utf-8 -*-
"""面试猴 · 语音助手引擎（engine 包）

按五段式架构实现：
    audio(音频采集) -> asr(语音转文本) -> rag(知识检索) -> llm(推理) -> answer(呈现给 GUI)

设计原则：
  1. 本地优先（Whisper/sherpa-onnx、Ollama、本地向量检索），支持离线兜底。
  2. 每个环节可插拔：装了对应依赖就用本地/在线引擎，没装就自动降级到无依赖实现。
  3. 无 API Key、无重依赖也能跑通全流程（规则化检索 + 抽取式摘要兜底）。
"""
from .assistant import Assistant

__all__ = ["Assistant"]
__version__ = "1.0.0"
