# -*- coding: utf-8 -*-
"""面试猴 · 桌面应用启动器（pywebview + WebView2）
把 renderer/ 装进原生窗口；会议/练习记录用 localStorage 持久化到本地目录。
语音助手引擎（engine）通过 js_api 暴露给渲染端。
"""
import os
import sys
import json
import webview


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def data_dir():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "InterviewMonkey")
    os.makedirs(d, exist_ok=True)
    return d


class Api:
    """暴露给渲染端的受控 API（类比 Electron 的 contextBridge）"""

    def __init__(self):
        from engine import Assistant
        from engine import llm as llm_mod
        self.cfg_path = os.path.join(data_dir(), "config.json")
        self.cfg = self._load_cfg()
        backend = llm_mod.build_llm_from_config(self.cfg)
        self.assistant = Assistant(
            knowledge_dir=resource_path(os.path.join("engine", "knowledge")),
            llm_backend=backend,
            bank_path=resource_path(os.path.join("renderer", "bank.js")),
        )

    def _load_cfg(self):
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)

    def ask(self, text):
        """智能问答：RAG 检索 + LLM 推理。"""
        try:
            answer, contexts = self.assistant.ask(text)
        except Exception as e:
            return {
                "answer": "调用失败：%s" % e,
                "sources": [],
                "status": self.assistant.status(),
                "error": True,
            }
        return {
            "answer": answer,
            "sources": [{"title": c["title"], "text": (c["text"] or "")[:200]} for c in contexts],
            "status": self.assistant.status(),
            "error": False,
        }

    def evaluate_answer(self, question, ref_points, answer):
        """面试陪练：回答评分 + 优化建议 + 参考答案。"""
        return self.assistant.evaluate_answer(question, ref_points or [], answer or "")

    def ai_status(self):
        return self.assistant.status()

    def get_config(self):
        from engine import llm as llm_mod
        return {
            "provider": self.cfg.get("provider") or "auto",
            "has_key": bool(self.cfg.get("api_key")),
            "model": self.cfg.get("model") or "",
            "ollama_model": self.cfg.get("ollama_model") or "",
            "ollama": llm_mod.ollama_available(),
            "ollama_models": llm_mod.ollama_models(),
            "llm": type(self.assistant.llm).__name__,
            "llm_label": llm_mod.describe(self.assistant.llm),
        }

    def save_config(self, cfg):
        from engine import llm as llm_mod
        cfg = cfg or {}
        provider = (cfg.get("provider") or "auto").strip().lower()
        api_key = (cfg.get("api_key") or "").strip()
        model = (cfg.get("model") or "").strip()
        ollama_model = (cfg.get("ollama_model") or "").strip()

        # 选 Ollama 时，界面上的「模型名」即 ollama 模型名
        if provider == "ollama" and model and not ollama_model:
            ollama_model, model = model, ""
        # 没送 ollama_model 就沿用原值，绝不写成硬编码的默认模型（旧 bug：会覆盖成未安装的 7b）
        if not ollama_model:
            ollama_model = self.cfg.get("ollama_model") or ""
        # Key 与服务商不匹配时按 Key 纠偏（智谱 Key 不是 sk- 开头）
        guess = llm_mod.detect_provider_by_key(api_key) if api_key else None
        if guess and provider in ("deepseek", "openai") and guess != provider:
            provider, model = guess, ""

        self.cfg = {
            "provider": provider,
            "api_key": api_key if api_key else (self.cfg.get("api_key") or ""),
            "model": model,
            "ollama_model": ollama_model,
        }
        self._save_cfg()
        self.assistant.llm = llm_mod.build_llm_from_config(self.cfg)
        return self.get_config()


    def toggle_topmost(self):
        w = webview.windows[0]
        w.on_top = not getattr(w, "on_top", False)
        return bool(w.on_top)


def main():
    html = resource_path(os.path.join("renderer", "index.html"))
    webview.create_window(
        "面试猴 · 会议实时总结助手",
        html,
        js_api=Api(),
        width=1160,
        height=780,
        min_size=(920, 600),
        background_color="#f3efe8",
    )
    # storage_path 让 localStorage 持久化，保证记录跨会话保留
    webview.start(storage_path=data_dir(), private_mode=False)


if __name__ == "__main__":
    main()
