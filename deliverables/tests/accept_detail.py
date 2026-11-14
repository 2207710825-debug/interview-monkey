# -*- coding: utf-8 -*-
"""阶段 P7 验收 (b)：详细模式真答「如何提升目标检测的精度」。

走与桌面 App 完全相同的链路：meeting_prompt.build(mode="detail") ->
assistant.ask_extract_stream(num_predict=1500) -> 流式解析拆出问题与回答。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine import llm as llm_mod
from engine import meeting_prompt as mp
from engine.assistant import Assistant
from engine.config_store import ConfigStore

APPDATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                       "InterviewMonkey")
cfg = ConfigStore(os.path.join(APPDATA, "config.json")).as_dict()
backend = llm_mod.build_llm_from_config(cfg)
print("后端:", llm_mod.describe(backend))

assistant = Assistant(llm_backend=backend)

recent = ["我们这次做的是小目标检测，行人比较多",
          "现在 mAP 卡在 70 出头上不去",
          "那你觉得如何提升目标检测的精度"]
raw = recent[-1]
ctxs = assistant._contexts(raw)
prompt = mp.build(recent=recent, last_question="mAP 的计算方式是什么",
                  ctx=ctxs, hotwords=mp.load_hotwords(), mode="detail")

print("=" * 70)
print("[P7] mode=detail num_predict=1500")
print("[P7] prompt[:300]=")
print(prompt[:300])
print("=" * 70)

events = []
parsed = assistant.ask_extract_stream(
    prompt, contexts=ctxs,
    on_event=lambda ev, *a: events.append((ev,) + a),
    num_predict=1500)

answer = parsed["answer"]
headings = [ln.strip() for ln in answer.splitlines()
            if ln.strip().startswith("##")]
body_chars = len(answer.replace(" ", "").replace("\n", ""))

print("\n【问题标题】", parsed["question"])
print("【回答全文】")
print(answer)
print("=" * 70)
print("小节标题数: %d  %s" % (len(headings), headings))
print("回答字数(去空白): %d" % body_chars)
print("delta 事件数: %d" % sum(1 for e in events if e[0] == "delta"))
print("验收: 5 个小节标题 ->", "PASS" if len(headings) >= 5 else "FAIL",
      "| 500 字以上 ->", "PASS" if body_chars >= 500 else "FAIL")
