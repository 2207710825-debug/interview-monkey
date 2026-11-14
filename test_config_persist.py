# -*- coding: utf-8 -*-
"""配置持久化测试：保存 -> 模拟重启 -> 自动加载 -> Key 不丢且脱敏"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.config_store import ConfigStore


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "config.json")

    # 1) 首次启动：无文件也要能就绪
    s1 = ConfigStore(path)
    print("[1] 首次启动默认值:", s1.as_dict())
    assert s1.get("provider") == "auto" and s1.get("top_k") == 3

    # 2) 用户填配置并保存
    s1.save({"provider": "deepseek", "api_key": "sk-abcdef1234567890wxyz",
             "model": "deepseek-chat", "top_k": 5})
    print("[2] 保存后:", {k: v for k, v in s1.as_dict().items() if k != "api_key"})
    assert os.path.isfile(path)

    # 3) 模拟重启：新实例读盘
    s2 = ConfigStore(path)
    print("[3] 重启后 provider=%s model=%s top_k=%s has_key=%s"
          % (s2.get("provider"), s2.get("model"), s2.get("top_k"), bool(s2.get("api_key"))))
    assert s2.get("api_key") == "sk-abcdef1234567890wxyz", "Key 必须完整恢复"
    assert s2.get("top_k") == 5

    # 4) 界面拿到的是脱敏值
    m = s2.masked()
    print("[4] 界面视图 key_masked=%r has_key=%s" % (m["key_masked"], m["has_key"]))
    assert "abcdef1234567890" not in json.dumps(m, ensure_ascii=False), "明文 Key 不能下发前端"

    # 5) 只改 top_k，不传 Key -> Key 不能丢
    s2.save({"top_k": 2})
    assert ConfigStore(path).get("api_key") == "sk-abcdef1234567890wxyz"
    print("[5] 只改 top_k 后 Key 仍保留 ✓ top_k=%s" % s2.get("top_k"))

    # 6) 老版本配置（缺字段）向前兼容
    old = os.path.join(tmp, "old.json")
    with open(old, "w", encoding="utf-8") as f:
        json.dump({"provider": "ollama", "ollama_model": "qwen2.5:3b"}, f)
    s3 = ConfigStore(old)
    print("[6] 老配置兼容:", {k: s3.get(k) for k in ("provider", "ollama_model", "top_k", "base_url")})
    assert s3.get("top_k") == 3 and s3.get("base_url") == ""

    # 7) 配置文件损坏时静默兜底，不影响启动
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON")
    s4 = ConfigStore(bad)
    print("[7] 损坏配置兜底:", s4.get("provider"), "✓")
    assert s4.get("provider") == "auto"

    print("\n全部断言通过 ✓")


if __name__ == "__main__":
    main()
