# -*- coding: utf-8 -*-
"""404/401 修复回归测试

覆盖：
  A. provider=ollama 但配置了未安装的 qwen2.5:7b  -> 应自动改用已安装模型，不再 404
  B. provider=deepseek 但粘贴的是智谱 Key        -> 应自动纠偏到智谱，不再 401
  C. 状态标签必须反映真实后端（不能一律显示 Ollama）
  D. 在线模型彻底失败时，问答应降级兜底而不是抛异常
"""
import os, sys, json, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import llm as L
from engine.assistant import Assistant

FAIL = []

def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)

def main():
    print("已安装 Ollama 模型:", L.ollama_models())

    # A. 未安装模型自动降级
    be = L.build_llm_from_config({"provider": "ollama", "ollama_model": "qwen2.5:7b"})
    check("A1 仍是 Ollama 后端", isinstance(be, L.OllamaBackend), type(be).__name__)
    check("A2 模型已自动换成已安装的", be.model in L.ollama_models(), be.model)
    a = be.generate("一句话说明什么是交并比 IoU")
    check("A3 生成成功", bool(a and len(a) > 2), a[:80])
    print("    标签:", be.label)

    # B. Key 格式自动纠偏
    # 仅用于验证 Key 格式识别的虚构样例；不得放入真实凭据。
    zkey = "0123456789abcdef0123456789abcdef.ABCDEFGHIJKLMNOP"
    check("B1 识别出智谱 Key", L.detect_provider_by_key(zkey) == "zhipu", L.detect_provider_by_key(zkey))
    be2 = L.build_llm_from_config({"provider": "deepseek", "api_key": zkey, "model": ""})
    check("B2 自动切到智谱接口", isinstance(be2, L.OpenAICompatBackend)
          and "bigmodel" in be2.base_url, getattr(be2, "base_url", ""))
    check("B3 模型为 glm-4-flash", getattr(be2, "model", "") == "glm-4-flash", getattr(be2, "model", ""))
    if os.environ.get("MH_RUN_ONLINE_TESTS") == "1" and os.environ.get("MH_ZHIPU_TEST_API_KEY"):
        live_backend = L.build_llm_from_config({
            "provider": "deepseek",
            "api_key": os.environ["MH_ZHIPU_TEST_API_KEY"],
            "model": "",
        })
        try:
            out = live_backend.generate("用一句话说明什么是交并比 IoU")
            check("B4 在线调用成功", bool(out and len(out) > 2), out[:80])
        except Exception as e:
            check("B4 在线调用成功", False, e)
    else:
        print("SKIP B4 在线调用：设置 MH_RUN_ONLINE_TESTS=1 和 MH_ZHIPU_TEST_API_KEY 后显式启用")

    # C. 状态标签
    asst = Assistant(knowledge_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "engine", "knowledge"),
                     llm_backend=be)
    st = asst.status()
    check("C1 状态带真实标签", "Ollama" in st["llm_label"], st["llm_label"])
    check("C2 状态带已装模型列表", isinstance(st["ollama_models"], list) and st["ollama_models"], st["ollama_models"])
    asst.llm = be2
    check("C3 在线后端标签正确", "智谱" in asst.status()["llm_label"], asst.status()["llm_label"])

    # D. 失败降级不抛异常
    class Boom:
        label = "坏掉的模型"
        def generate(self, prompt):
            raise L.LLMError("API Key 无效或与所选服务商不匹配（HTTP 401）。")
    asst.llm = Boom()
    ans, ctxs = asst.ask("什么是 IoU？")
    check("D1 问答未抛异常且给出兜底", isinstance(ans, str) and "调用失败" in ans, ans[:120])

    print("\n结果:", "全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL))
    return 0 if not FAIL else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
