# -*- coding: utf-8 -*-
"""流式首字延迟基准：用真实后端依次问 5 个固定问题，打印每题 first_token_ms。

运行：
    python tests/bench_latency.py

后端选择与桌面 App 完全一致（读本机保存的配置 -> build_llm_from_config）。
没有 API Key、也没装 Ollama 时，Assistant 会自动降级到规则兜底；
脚本仍会跑完并如实打印，末尾标注本次是「离线兜底」而非真实模型延迟。
"""
import os
import sys
import time

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from engine import llm as llm_mod
from engine import assistant as asst_mod
from engine.config_store import ConfigStore

QUESTIONS = [
    "什么是 IoU 和 CIoU，两者区别在哪？",
    "为什么目标检测里要用 NMS，它有什么缺点？",
    "讲讲 Transformer 里自注意力的计算原理",
    "简历里的项目经历怎么写才不显得空？",
    "系统设计：如何设计一个短链服务？",
]


def build_assistant():
    data_dir = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "InterviewMonkey")
    cfg = ConfigStore(os.path.join(data_dir, "config.json")).as_dict()
    backend = llm_mod.build_llm_from_config(cfg)
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    a = asst_mod.Assistant(
        knowledge_dir=os.path.join(root, "engine", "knowledge"),
        llm_backend=backend,
        bank_path=os.path.join(root, "renderer", "bank.js"),
        top_k=cfg.get("top_k", 3),
    )
    return a, backend


def main():
    a, backend = build_assistant()
    print("后端：%s" % getattr(backend, "label", type(backend).__name__))
    print("固定问题数：%d\n" % len(QUESTIONS))
    lats = []
    for i, q in enumerate(QUESTIONS, 1):
        t0 = time.time()
        first_ms = None
        err = ""

        def on_chunk(piece, acc):
            nonlocal first_ms
            if first_ms is None and (piece or "").strip():
                first_ms = int((time.time() - t0) * 1000)

        try:
            full, _ = a.ask_stream(q, recent=None, mode="normal", on_chunk=on_chunk)
            if first_ms is None and (full or "").strip():
                first_ms = int((time.time() - t0) * 1000)
        except Exception as e:
            err = " ERROR: %s" % e
            full = ""
        total_ms = int((time.time() - t0) * 1000)
        if first_ms is not None:
            lats.append(first_ms)
        print("[%d/%d] first_token_ms=%-6s total_ms=%-6s answer_len=%-4d %s%s"
              % (i, len(QUESTIONS),
                 first_ms if first_ms is not None else "n/a",
                 total_ms, len(full or ""), q, err))

    print("")
    if lats:
        print("首字延迟：平均 %d ms · 最快 %d ms · 最慢 %d ms（%d/%d 题有输出）"
              % (sum(lats) // len(lats), min(lats), max(lats), len(lats), len(QUESTIONS)))
    else:
        print("本次没有任何首字输出。")
    if isinstance(a.llm, llm_mod.RuleBasedBackend):
        print("提示：当前走的是离线规则兜底，不是真实模型延迟。"
              "配置 Ollama 或在线 Key 后重跑即可得到真实数值。")
    if a.last_error:
        print("引擎最后一条提示：%s" % a.last_error[:200])


if __name__ == "__main__":
    main()
