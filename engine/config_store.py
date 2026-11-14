# -*- coding: utf-8 -*-
"""配置持久化（Config Store）

解决痛点：每次打开应用都要重新配置 API。

设计要点：
  1. 单一配置文件 config.json（%APPDATA%/InterviewMonkey/config.json），人类可读可手改
  2. 原子写入（先写临时文件再 os.replace），避免写一半断电/崩溃导致配置损坏
  3. schema 版本号 + 默认值合并：老配置文件缺新字段时自动补默认值，向前兼容
  4. Key 脱敏回显：界面永远拿不到完整 Key，只拿 "sk-****abcd"，防截图泄露
  5. 保存即校验：provider 与 Key 格式不符时自动纠偏（沿用 llm.detect_provider_by_key）

配置字段：
  version      : schema 版本（int）
  provider     : auto | ollama | deepseek | zhipu | openai
  api_key      : 在线模型 API Key（本地明文保存——数据不出本机，与「纯本地」定位一致）
  base_url     : 自定义 OpenAI 兼容端点（留空用服务商预设；vLLM/OneAPI 等自建网关填这里）
  model        : 在线模型名（留空用服务商预设）
  ollama_model : Ollama 本地模型名（留空自动挑已安装的最小模型）
  top_k        : 知识库检索条数（1~8，默认 3；调大召回多但 Prompt 变长、首字变慢）
"""
import os
import json
import tempfile

SCHEMA_VERSION = 2

# 全部字段的默认值：load 时把磁盘上缺的字段补齐，代码里永远不用再写 cfg.get(...) or ...
DEFAULTS = {
    "provider": "auto",
    "api_key": "",
    "base_url": "",
    "model": "",
    "ollama_model": "",
    "top_k": 3,
}


class ConfigStore:
    def __init__(self, path):
        self.path = path
        self._cfg = dict(DEFAULTS)
        self.load()

    # ---------- 读 ----------
    def load(self):
        """读盘 + 默认值合并。文件缺失/损坏时静默用默认值（首次启动即就绪）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if not isinstance(disk, dict):
                disk = {}
        except Exception:
            disk = {}
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in disk.items() if k in DEFAULTS})
        # 类型纠偏：top_k 必须是 1~8 的整数
        try:
            cfg["top_k"] = max(1, min(8, int(cfg["top_k"])))
        except (TypeError, ValueError):
            cfg["top_k"] = DEFAULTS["top_k"]
        cfg["version"] = SCHEMA_VERSION
        self._cfg = cfg
        return dict(self._cfg)

    # ---------- 写 ----------
    def save(self, patch):
        """合并式保存：只传要改的字段，其余沿用；立即原子落盘。"""
        cfg = dict(self._cfg)
        cfg.update({k: v for k, v in (patch or {}).items() if k in DEFAULTS})
        try:
            cfg["top_k"] = max(1, min(8, int(cfg.get("top_k", DEFAULTS["top_k"]))))
        except (TypeError, ValueError):
            cfg["top_k"] = DEFAULTS["top_k"]
        cfg["version"] = SCHEMA_VERSION
        self._cfg = cfg
        self._atomic_write(cfg)
        return dict(self._cfg)

    def _atomic_write(self, cfg):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="cfg_", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)   # 原子替换：要么旧配置要么新配置，不存在半截
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    # ---------- 取值 ----------
    def get(self, key, default=None):
        return self._cfg.get(key, default)

    def as_dict(self):
        return dict(self._cfg)

    # ---------- 脱敏 ----------
    @staticmethod
    def mask_key(key):
        """sk-abcdef1234...wxyz -> sk-****wxyz；空/过短返回空串。"""
        key = (key or "").strip()
        if not key:
            return ""
        if len(key) <= 8:
            return key[:2] + "****"
        return key[:4] + "****" + key[-4:]

    def masked(self):
        """给界面的脱敏视图。"""
        return {
            "provider": self._cfg["provider"],
            "has_key": bool(self._cfg["api_key"]),
            "key_masked": self.mask_key(self._cfg["api_key"]),
            "base_url": self._cfg["base_url"],
            "model": self._cfg["model"],
            "ollama_model": self._cfg["ollama_model"],
            "top_k": self._cfg["top_k"],
        }
