# -*- coding: utf-8 -*-
"""App Api 层端到端复现测试：完全走 App 真实调用链（配置 -> 引擎 -> 问答）"""
import os, sys, json, time, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("app_main", os.path.join(HERE, "面试猴.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

api = m.Api()
safe_cfg = dict(api.cfg)
safe_cfg["api_key"] = "[REDACTED]" if safe_cfg.get("api_key") else ""
print("配置:", json.dumps(safe_cfg, ensure_ascii=False))
print("状态:", json.dumps(api.ai_status(), ensure_ascii=False))

t0 = time.time()
r = api.ask("CIOU 的实现是什么？")
print("耗时: %.1fs" % (time.time() - t0))
print("error:", r.get("error"))
print("引擎:", r["status"]["llm_label"])
print("回答:", r["answer"][:400])

ok = (not r.get("error")) and len(r["answer"]) > 20 and "调用失败" not in r["answer"]
print("\n结果:", "PASS 问答链路正常" if ok else "FAIL 仍失败")
sys.exit(0 if ok else 1)
