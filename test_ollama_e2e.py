# -*- coding: utf-8 -*-
"""Ollama 端到端验证脚本"""
import sys, os, json, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    from engine.llm import build_llm_from_config

    appdata = os.environ.get("APPDATA", "")
    cfg_path = os.path.join(appdata, "InterviewMonkey", "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    safe_cfg = dict(cfg)
    safe_cfg["api_key"] = "[REDACTED]" if safe_cfg.get("api_key") else ""
    print("配置:", safe_cfg)

    llm = build_llm_from_config(cfg)
    print("LLM 类型:", type(llm).__name__)

    t0 = time.time()
    out = llm.generate("用一句话回答：什么是CNN中的感受野？")
    print("耗时: %.1fs" % (time.time() - t0))
    print("回答:", out[:400])

    # 评分引擎联动测试
    try:
        from engine.evaluate import rule_based_evaluate
        r = rule_based_evaluate(
            question="什么是CNN中的感受野？",
            ref_points=["感受野是神经元能看到输入区域的大小", "感受野随层数堆叠逐层增大"],
            answer="感受野是卷积神经网络中一个神经元能看到的输入区域大小，"
                   "它随卷积层和池化层的堆叠逐层增大，深层神经元的感受野覆盖更广的原始输入区域。",
        )
        print("评分结果:", json.dumps(r, ensure_ascii=False)[:400])
    except Exception:
        traceback.print_exc()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
