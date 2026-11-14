# -*- coding: utf-8 -*-
"""流式链路端到端测试（不依赖真实 Ollama / API Key）

起一个假的 Ollama 流式服务（NDJSON，逐字延时 60ms），
验证：首字延迟、逐字回调、完整聚合、[SKIP] 判定、降级链路。
"""
import json
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FakeOllama(BaseHTTPRequestHandler):
    reply = "交并比、召回率、NMS\n\n· 交并比衡量预测框与真值框的重叠程度\n· 召回率关注漏检，精度关注误检\n· NMS 用于去除重复检测框"
    char_delay = 0.06

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": "qwen2.5:3b"}, {"name": "qwen2.5:7b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode())
        # 模拟「这句话不是问题」：模型判定跳过
        skip = "今天天气不错" in json.dumps(req, ensure_ascii=False)
        text = "[SKIP]" if skip else self.reply
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for ch in text:
            line = json.dumps({"message": {"content": ch}, "done": False}, ensure_ascii=False)
            self.wfile.write((line + "\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(self.char_delay if not skip else 0.01)
        self.wfile.write((json.dumps({"done": True}) + "\n").encode())
        self.wfile.flush()


def main():
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    print("假 Ollama 监听:", base)

    from engine import llm as L
    from engine import Assistant

    # 1) 流式后端直连
    be = L.OllamaBackend(model="qwen2.5:3b", url=base)
    t0 = time.time()
    first = None
    chunks = []
    for piece in be.generate_stream("什么是 mAP"):
        if first is None:
            first = time.time() - t0
        chunks.append(piece)
    total = time.time() - t0
    print("\n[1] 流式后端: 首字 %.3fs / 全量 %.3fs / 片段数 %d" % (first, total, len(chunks)))
    assert len(chunks) > 10, "应该是逐字流式，不是一次性返回"
    assert first < 0.6, "首字延迟应显著小于全量耗时"
    assert "".join(chunks) == FakeOllama.reply
    print("    内容一致 ✓")

    # 2) 编排层：on_chunk 回调 + 聚合
    a = Assistant(knowledge_dir=None, llm_backend=be, bank_path=None, top_k=3)
    a.retriever = type("R", (), {"retrieve": lambda self, q, top_k=3: [], "index": type("I", (), {"chunks": []})()})()
    stamps = []
    t0 = time.time()
    full, _ = a.ask_stream("什么是 mAP", recent=["上一句"], on_chunk=lambda p, acc: stamps.append((time.time() - t0, p)))
    print("\n[2] 编排流式: 回调 %d 次, 首块 %.3fs, 答案前 20 字: %r"
          % (len(stamps), stamps[0][0], full[:20]))
    assert len(stamps) > 10 and stamps[0][0] < 1.5
    assert full.startswith("交并比")

    # 3) [SKIP] 判定：不能把 [SKIP] 漏到界面上
    seen = []
    full2, _ = a.ask_stream("今天天气不错", recent=["上一句"], on_chunk=lambda p, acc: seen.append(acc))
    print("\n[3] SKIP 判定: 返回=%r, 回调次数=%d" % (full2, len(seen)))
    assert full2 == "[SKIP]", "应识别为跳过"
    assert not any("[SKIP" in s for s in seen), "禁止把 [SKIP] 标记推到前端"
    print("    未泄漏标记 ✓")

    # 4) 降级链路：主后端不可达 -> 自动切兜底
    dead = L.OllamaBackend(model="qwen2.5:3b", url="http://127.0.0.1:1")
    a2 = Assistant(knowledge_dir=None, llm_backend=dead, bank_path=None)
    a2.retriever = a.retriever
    a2.bank = None
    txt, _ = a2.ask("什么是 mAP", recent=None)
    print("\n[4] 降级兜底: 正常返回 %d 字, 开头: %r" % (len(txt), txt[:40]))
    assert txt and not txt.startswith("[SKIP]")

    srv.shutdown()
    print("\n全部断言通过 ✓")


if __name__ == "__main__":
    main()
