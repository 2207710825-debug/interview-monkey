# -*- coding: utf-8 -*-
"""面试猴语音助手引擎 · 命令行演示

用法：
    python demo.py "面试猴有什么功能"         # 文本提问（零依赖即可跑）
    python demo.py "如何提问" --status        # 附带打印引擎状态

可选参数：
    --ollama-model qwen2.5:7b  指定 Ollama 模型
    --asr-model <目录>         指定 sherpa-onnx 模型目录（启用本地语音识别）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Assistant  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_status = "--status" in sys.argv
    ollama_model = "qwen2.5:7b"
    for i, a in enumerate(sys.argv):
        if a == "--ollama-model" and i + 1 < len(sys.argv):
            ollama_model = sys.argv[i + 1]

    from engine import llm as llm_mod
    backend = llm_mod.build_llm(ollama_model=ollama_model)
    a = Assistant(llm_backend=backend)

    question = " ".join(args).strip() or "面试猴有什么功能？"
    answer, contexts = a.ask(question)

    print("=" * 50)
    print("问题：", question)
    print("-" * 50)
    print("回答：\n", answer)
    if contexts:
        print("-" * 50)
        print("命中知识库片段：")
        for c in contexts:
            print("  · [%s] %s" % (c["title"], c["text"][:60]))
    if show_status:
        print("=" * 50)
        print("引擎状态：", a.status())
    print("=" * 50)


if __name__ == "__main__":
    main()
