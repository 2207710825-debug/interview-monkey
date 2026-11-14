# -*- coding: utf-8 -*-
"""面试猴 · 桌面应用启动器（pywebview + WebView2）

把 renderer/ 装进原生窗口；会议/练习记录用 localStorage 持久化到本地目录。
语音助手引擎（engine）通过 js_api 暴露给渲染端。

两个体验优化：
  1. 配置持久化（engine/config_store.py）：Key / BaseURL / 模型 / 检索参数落盘，
     启动自动读取 + Ollama 连通性检测 + 自动切换，界面直接就绪。
  2. 流式输出：js_api 是「请求-响应」模式，拿不到增量。
     这里用后台线程调 ask_stream，每来一段调 window.evaluate_js 推给前端，
     实现首字即现的打字机效果。
"""
import os
import sys
import json
import threading
import webview

APP_NAME = "面试猴"


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def data_dir():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "InterviewMonkey")
    os.makedirs(d, exist_ok=True)
    return d


# ---------- 前端推送：把 Python 侧的字符串安全塞进 JS ----------
def _js_str(s):
    """用 JSON 编码成 JS 字符串字面量，自动处理引号/换行/反斜杠/中文。

    不用手动 replace 转义——那是最容易出「回答里带引号就白屏」的地方。
    """
    return json.dumps(s if s is not None else "", ensure_ascii=False)


def _push(js_code):
    """在主线程之外安全地执行一段 JS（pywebview 的 evaluate_js 线程安全）。"""
    try:
        if webview.windows:
            webview.windows[0].evaluate_js(js_code)
    except Exception:
        pass


class Api:
    """暴露给渲染端的受控 API（类比 Electron 的 contextBridge）"""

    def __init__(self):
        from engine import Assistant
        from engine import llm as llm_mod
        from engine.config_store import ConfigStore

        self.store = ConfigStore(os.path.join(data_dir(), "config.json"))
        self.cfg = self.store.as_dict()
        backend = llm_mod.build_llm_from_config(self.cfg)
        self.assistant = Assistant(
            knowledge_dir=resource_path(os.path.join("engine", "knowledge")),
            llm_backend=backend,
            bank_path=resource_path(os.path.join("renderer", "bank.js")),
            top_k=self.cfg.get("top_k", 3),
        )
        self._stream_lock = threading.Lock()
        self._stream_seq = 0        # 每次提问自增，前端据此丢弃过期片段
        self._boot_state = {"ready": False, "note": ""}

    # ================= 配置：读 / 写 / 启动检测 =================
    def _save_cfg(self):
        # ConfigStore 内部是原子写，这里保留方法名以兼容旧调用
        self.cfg = self.store.save(self.cfg)

    def get_config(self):
        """给界面的配置视图：Key 只回脱敏值，永不下发明文。

        界面据此把「已保存」状态画出来——用户打开就看到 Key 已填好，
        不必重新粘一遍（痛点 1 的直观感受来源）。
        """
        from engine import llm as llm_mod
        view = self.store.masked()
        view.update({
            "ollama": llm_mod.ollama_available(),
            "ollama_models": llm_mod.ollama_models(),
            "llm": type(self.assistant.llm).__name__,
            "llm_label": llm_mod.describe(self.assistant.llm),
            "config_path": self.store.path,
            "boot_note": self._boot_state.get("note", ""),
            "ready": self._boot_state.get("ready", False),
        })
        return view

    def save_config(self, cfg):
        """保存配置：合并式写入 + 立刻重建后端 + 返回新视图（界面可直接刷新）。"""
        from engine import llm as llm_mod
        cfg = dict(cfg or {})

        # 界面没改 Key 时传空串 -> 沿用已存的 Key（关键：不能让「不重填」把 Key 抹掉）
        api_key = (cfg.get("api_key") or "").strip()
        if not api_key:
            api_key = self.store.get("api_key") or ""

        provider = (cfg.get("provider") or "auto").strip().lower()
        model = (cfg.get("model") or "").strip()
        ollama_model = (cfg.get("ollama_model") or "").strip()
        base_url = (cfg.get("base_url") or "").strip()

        # 选 Ollama 时，界面上的「模型名」即 ollama 模型名
        if provider == "ollama" and model and not ollama_model:
            ollama_model, model = model, ""
        # Key 与服务商不匹配时按 Key 格式纠偏（智谱 Key 不是 sk- 开头）
        guess = llm_mod.detect_provider_by_key(api_key) if api_key else None
        if guess and provider in ("deepseek", "openai") and guess != provider:
            provider, model = guess, ""

        patch = {
            "provider": provider,
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "ollama_model": ollama_model,
        }
        if "top_k" in cfg:
            patch["top_k"] = cfg.get("top_k")

        self.cfg = self.store.save(patch)
        self.assistant.top_k = self.cfg.get("top_k", 3)
        self.assistant.llm = llm_mod.build_llm_from_config(self.cfg)
        view = self.get_config()
        view["saved"] = True
        return view

    def probe_backends(self):
        """连通性检测：Ollama 是否在线 + 已装哪些模型 + 在线 Key 是否配好。

        启动时与「模型设置」里手动刷新都走这里。
        """
        from engine import llm as llm_mod
        return {
            "ollama": llm_mod.ollama_available(),
            "ollama_models": llm_mod.ollama_models(),
            "ollama_url": llm_mod._OLLAMA_URL,
            "has_key": bool(self.store.get("api_key")),
            "llm_label": llm_mod.describe(self.assistant.llm),
        }

    def auto_pick_provider(self):
        """auto 模式下自动选路：本地 Ollama 可用 -> 走本地（低延迟、离线）；
        否则有 Key 走在线；都没有则规则兜底。

        这就是「启动时检测到本地 Ollama 就自动切换」的落点。
        """
        from engine import llm as llm_mod
        if (self.store.get("provider") or "auto") != "auto":
            return self.get_config()
        backend = llm_mod.build_llm_from_config(self.cfg)
        self.assistant.llm = backend
        return self.get_config()

    def bootstrap(self):
        """启动自检（前端 init 时调一次）。

        1) 配置已加载（ConfigStore 构造时完成）
        2) 探测 Ollama
        3) auto 模式下按可用性自动切换后端
        4) 返回一句人类可读的就绪说明，界面顶部直接显示
        """
        from engine import llm as llm_mod
        self.auto_pick_provider()
        ollama_ok = llm_mod.ollama_available()
        models = llm_mod.ollama_models()
        has_key = bool(self.store.get("api_key"))

        if isinstance(self.assistant.llm, llm_mod.OllamaBackend):
            note = "本地 Ollama 已就绪 · %s" % self.assistant.llm.model
        elif isinstance(self.assistant.llm, llm_mod.OpenAICompatBackend):
            note = "在线模型已就绪 · %s" % self.assistant.llm.model
            if ollama_ok and models:
                note += "（本地 Ollama 也可用）"
        else:
            note = "未接入大模型：规则知识库兜底（点「模型设置」配一次即可）"
            if ollama_ok:
                note = "检测到 Ollama 但无可用模型，请先 ollama pull"

        self._boot_state = {"ready": True, "note": note}
        view = self.get_config()
        view["boot_note"] = note
        view["ready"] = True
        view["has_key"] = has_key
        return view

    # ================= 问答：流式 =================
    def ask_stream(self, text, recent=None):
        """流式问答（非阻塞）。

        立即返回 {"stream_id": N}，随后在后台线程里把增量通过
        window.__onChunk(stream_id, piece, full) 推给前端，
        结束时推 __onDone / __onSkip。
        """
        with self._stream_lock:
            self._stream_seq += 1
            sid = self._stream_seq

        def _worker():
            try:
                full, contexts = self.assistant.ask_stream(
                    text, recent=recent,
                    on_chunk=lambda piece, acc: _push(
                        "window.__onChunk&&window.__onChunk(%d,%s,%s)"
                        % (sid, _js_str(piece), _js_str(acc))
                    ),
                )
                if (full or "").strip().startswith("[SKIP]"):
                    _push("window.__onSkip&&window.__onSkip(%d)" % sid)
                    return
                srcs = [{"title": c["title"], "text": (c["text"] or "")[:200]}
                        for c in (contexts or [])]
                _push("window.__onDone&&window.__onDone(%d,%s,%s,%s)" % (
                    sid, _js_str(full), _js_str(json.dumps(srcs, ensure_ascii=False)),
                    _js_str(self.assistant.last_error or ""),
                ))
            except Exception as e:
                _push("window.__onError&&window.__onError(%d,%s)" % (sid, _js_str(str(e))))

        threading.Thread(target=_worker, daemon=True).start()
        return {"stream_id": sid, "llm": type(self.assistant.llm).__name__}

    def ask(self, text, recent=None):
        """非流式问答（保留给内部/测试用；界面走 ask_stream）。"""
        try:
            answer, contexts = self.assistant.ask(text, recent=recent)
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
        from engine import asr as asr_mod
        st = self.assistant.status()
        st["asr_offline"] = asr_mod.available()
        return st

    # ================= 离线流式语音识别 =================
    # （WebView2 里 Web Speech 走 Google 云，必失败，故用内置 sherpa-onnx）
    def asr_start(self):
        from engine import asr as asr_mod
        try:
            if getattr(self, "_asr_engine", None) is None:
                self._asr_engine = asr_mod.build_asr("sherpa")
            if self._asr_engine is None:
                return {"ok": False, "error": "离线语音模型不可用"}
            self._asr_sess = asr_mod.StreamingSession(self._asr_engine)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def preheat_asr(self):
        """启动时后台预热离线识别模型（双语模型约 2 秒），
        用户第一次按「开始说话」就无需等待加载。"""
        def _run():
            try:
                from engine import asr as asr_mod
                if asr_mod.available() and getattr(self, "_asr_engine", None) is None:
                    self._asr_engine = asr_mod.build_asr("sherpa")
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def preheat_llm(self):
        """启动时后台预热本地大模型。

        主后端是 Ollama 时预热它；是在线后端时也预热本地兜底——
        因为在线 Key 欠费/断网时会自动降级到本地，提前加载好（3b 冷加载约数十秒），
        用户说话时才不会卡一两分钟。
        """
        def _run():
            try:
                from engine import llm as L
                warm = None
                if isinstance(self.assistant.llm, L.OllamaBackend):
                    warm = self.assistant.llm
                elif L.ollama_available() and L.ollama_models():
                    warm = L.OllamaBackend(None)
                if warm is not None:
                    warm.generate("好")  # 极短请求，只为把模型加载进内存
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def asr_feed(self, b64_pcm):
        """接收一段 16k 单声道 int16 PCM（base64）。

        返回 {"text": 当前累计文本, "final": 刚刚说完的那句（没有则为 None）}。
        final 是一次性的：取走后置空，前端据此触发「自动提问」。
        """
        import base64
        sess = getattr(self, "_asr_sess", None)
        if sess is None:
            return {"text": "", "final": None}
        try:
            text = sess.feed(base64.b64decode(b64_pcm))
            final, sess._pending = sess._pending, None
            return {"text": text, "final": final}
        except Exception:
            return {"text": "", "final": None}

    def asr_stop(self):
        """结束本次监听，返回 {"text": 全文, "final": 最后一句（可能为 None）}。"""
        sess = getattr(self, "_asr_sess", None)
        text, final = "", None
        try:
            if sess is not None:
                text = sess.finish()
                final, sess._pending = sess._pending, None
        except Exception:
            text, final = "", None
        finally:
            self._asr_sess = None
        return {"text": text, "final": final}

    # ================= localStorage 源迁移（file:// -> http://127.0.0.1） =================
    def export_legacy(self, dump):
        try:
            with open(os.path.join(data_dir(), "legacy_dump.json"), "w", encoding="utf-8") as f:
                json.dump(dump or {}, f, ensure_ascii=False)
            return True
        except Exception:
            return False

    def take_legacy(self):
        p = os.path.join(data_dir(), "legacy_dump.json")
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        try:
            os.replace(p, p + ".imported")
        except Exception:
            pass
        return data

    def toggle_topmost(self):
        w = webview.windows[0]
        w.on_top = not getattr(w, "on_top", False)
        return bool(w.on_top)


def main():
    # WebView2 会继承系统代理，导致 127.0.0.1 也被代理转发、页面打不开；
    # 必须让浏览器进程绕过代理，才能用本地 http 提供服务（安全上下文）。
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--no-proxy-server")

    html = resource_path(os.path.join("renderer", "index.html"))
    api = Api()
    api.preheat_asr()
    api.preheat_llm()
    webview.create_window(
        "面试猴 · 会议实时总结助手",
        html,
        js_api=api,
        width=1160,
        height=780,
        min_size=(920, 600),
        background_color="#f3efe8",
    )
    # storage_path 让 localStorage 持久化，保证记录跨会话保留。
    # pywebview 6 默认用 http://127.0.0.1 提供页面（是安全上下文），麦克风因此可用；
    # 这里不指定端口，避免换端口导致 localStorage 源变化、历史记录"消失"。
    webview.start(storage_path=data_dir(), private_mode=False)


if __name__ == "__main__":
    main()
