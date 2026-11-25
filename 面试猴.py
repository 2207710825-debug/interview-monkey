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
import time
import threading
import webview

from engine.window_security import WindowSecurityGuard


def _log(msg):
    """P7 调试日志：写 %APPDATA%/InterviewMonkey/app.log 并同步 stdout。

    桌面 exe 没有 console，print 看不到，所以必须落文件；用户要核对
    最终 prompt 前 300 字与 num_predict，就到这里看。
    """
    line = "%s %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
    try:
        with open(os.path.join(data_dir(), "app.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def data_dir():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "InterviewMonkey")
    os.makedirs(d, exist_ok=True)
    return d


# ---------- WebView2 运行时自检 ----------
def check_webview2():
    """在启动窗口前确认 WebView2 运行时可用，返回 (ok, 版本或错误说明)。

    为什么需要这段：pywebview 的 .NET 包装层在部分新版本 Windows 上
    （实测 Windows 11 Build 26200 + pywebview 6.2.1）会抛出
    "Couldn't find a compatible Webview2 Runtime"，尽管运行时本身完全正常
    （同一台机器用原生 COM API 创建环境可以成功）。这种情况如果直接
    webview.start()，用户看到的是「窗口一闪而过」或长时间无响应，
    完全无从下手。这里先探测，失败时在控制台给出可执行的指引。

    探测手段用 WebView2Loader 的 C API，不依赖 .NET 包装层，
    因此不会受上述兼容性问题影响。
    """
    try:
        import ctypes
        from ctypes import wintypes
        import webview as _wv

        # loader 与 .NET 包装器同目录，pywebview 的 lib/ 下就有
        lib_dir = os.path.join(os.path.dirname(os.path.abspath(_wv.__file__)), "lib")
        candidates = [
            os.path.join(lib_dir, "runtimes", "win-x64", "native", "WebView2Loader.dll"),
            os.path.join(lib_dir, "runtimes", "win-x86", "native", "WebView2Loader.dll"),
            os.path.join(lib_dir, "WebView2Loader.dll"),
        ]
        loader = next((p for p in candidates if os.path.isfile(p)), None)
        if not loader:
            return True, ""      # 探测不了就不阻拦，交给 pywebview 自己报错

        dll = ctypes.WinDLL(loader)
        fn = dll.GetAvailableCoreWebView2BrowserVersionString
        fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)]
        fn.restype = ctypes.c_long
        ver = wintypes.LPWSTR()
        hr = fn(None, ctypes.byref(ver))
        if hr == 0 and ver.value:
            return True, ver.value
        return False, "未检测到可用的 WebView2 运行时（HRESULT=0x%08X）" % (hr & 0xFFFFFFFF)
    except Exception as e:
        return True, ""          # 探测本身出错不应阻断启动



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

    def __init__(self, realtime_url=""):
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
        self._window_maximized = False

        # ---- 持久化存储（sqlite3，WAL 模式）----
        from engine import store as store_mod
        self._db = store_mod.connect()
        self._cur_sid = None        # 当前活跃会话 id（None 表示尚未开始）

        # ---- 语音聚合器（缓冲定稿句，静默达标才合并提问）----
        from engine import pipeline as pipe_mod
        self._ai_mode = "normal"   # 当前回答模式（前端切换时更新）
        self._aggregator = pipe_mod.QuestionAggregator(
            on_question=self._on_aggregated_question,
        )

        # ---- 流式取消管理 ----
        self._cur_cancel = None    # 当前在跑的生成任务的取消信号（threading.Event）
        self._cancel_lock = threading.Lock()

        # ---- 回环采集（系统播放的声音）----
        self._loop_cap = None      # LoopbackCapture 实例
        self._loop_q = None        # 音频回调 -> 工作线程的 PCM 队列
        self._loop_stop = None     # 工作线程退出信号
        self._loop_sess = None     # TwoPassASR / 兜底识别器
        self._loop_merger = None   # 只按静默合并，判定交给两段式提示词
        self._loop_recent = []     # 对方最近说的话（旧→新）
        self._loop_last_question = None
        self._loop_hotwords = ""
        # 设备热插拔监听（插拔耳机 / 切换默认输出设备时自动重开回环）
        self._cap_watch_stop = None
        self._cap_device_sig = None
        self._cap_on_pcm = None
        self._cap_device_index = None
        self._realtime_url = (realtime_url or "").strip()

    def get_realtime_url(self):
        """返回嵌入“智能问答”页的本地实时服务地址。"""
        return {"ok": bool(self._realtime_url), "url": self._realtime_url}

    # ================= 配置：读 / 写 / 启动检测 =================
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
    def ask_async(self, qa_id, text, mode="normal", recent=None):
        """异步流式问答（带取消机制）。

        起后台线程生成，通过 evaluate_js 调前端：
          onAnswerDelta(qa_id, piece) / onAnswerDone(qa_id, stats)
        新问题到来时取消上一条——已生成的部分保留，status 标 "interrupted"。
        """
        # 取消上一条在跑的生成
        with self._cancel_lock:
            prev = self._cur_cancel
            if prev is not None:
                prev.set()           # 触发上一条的取消信号
            cancel_evt = threading.Event()
            self._cur_cancel = cancel_evt

        def _worker():
            import time as _time
            from engine import store as store_mod
            t0 = _time.time()
            first_ms = None
            try:
                def _on_chunk(piece, acc):
                    nonlocal first_ms
                    if first_ms is None and (piece or "").strip():
                        first_ms = int((_time.time() - t0) * 1000)
                    # 流式片段追加写入 answer（只追加不覆盖）
                    if qa_id is not None:
                        store_mod.append_qa_answer(self._db, qa_id, piece)
                    _push("window.onAnswerDelta&&window.onAnswerDelta(%d,%s)"
                          % (qa_id or 0, _js_str(piece)))

                full, contexts = self.assistant.ask_stream(
                    text, recent=recent, mode=mode,
                    on_chunk=_on_chunk, cancel=cancel_evt,
                )
                # 被取消：部分答案已落库，标 interrupted
                if cancel_evt.is_set():
                    if qa_id is not None:
                        store_mod.finish_qa(self._db, qa_id, status="interrupted",
                                            first_token_ms=first_ms,
                                            total_ms=int((_time.time()-t0)*1000))
                    _push("window.onAnswerDone&&window.onAnswerDone(%d,%s)"
                          % (qa_id or 0, _js_str(json.dumps(
                              {"status": "interrupted",
                               "first_token_ms": first_ms,
                               "total_ms": int((_time.time()-t0)*1000)},
                              ensure_ascii=False))))
                    return
                # 正常完成
                if (full or "").strip().startswith("[SKIP]"):
                    if qa_id is not None:
                        store_mod.finish_qa(self._db, qa_id, status="skip",
                                            first_token_ms=first_ms,
                                            total_ms=int((_time.time()-t0)*1000))
                    _push("window.onAnswerDone&&window.onAnswerDone(%d,%s)"
                          % (qa_id or 0, _js_str(json.dumps(
                              {"status": "skip"}, ensure_ascii=False))))
                    return
                srcs = [{"title": c["title"], "text": (c["text"] or "")[:200]}
                        for c in (contexts or [])]
                if qa_id is not None:
                    store_mod.finish_qa(self._db, qa_id, status="done",
                                        first_token_ms=first_ms,
                                        total_ms=int((_time.time()-t0)*1000),
                                        sources_json=json.dumps(srcs, ensure_ascii=False))
                _push("window.onAnswerDone&&window.onAnswerDone(%d,%s)"
                      % (qa_id or 0, _js_str(json.dumps(
                          {"status": "done",
                           "first_token_ms": first_ms,
                           "total_ms": int((_time.time()-t0)*1000),
                           "sources": srcs},
                          ensure_ascii=False))))
            except Exception as e:
                if qa_id is not None:
                    store_mod.finish_qa(self._db, qa_id, status="error",
                                        first_token_ms=first_ms,
                                        total_ms=int((_time.time()-t0)*1000))
                _push("window.onAnswerDone&&window.onAnswerDone(%d,%s)"
                      % (qa_id or 0, _js_str(json.dumps(
                          {"status": "error", "msg": str(e)},
                          ensure_ascii=False))))
            finally:
                with self._cancel_lock:
                    if self._cur_cancel is cancel_evt:
                        self._cur_cancel = None

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True}

    def ask_stream(self, text, recent=None, mode="normal"):
        """流式问答（非阻塞）。

        立即返回 {"stream_id": N}，随后在后台线程里把增量通过
        window.__onChunk(stream_id, piece, full) 推给前端，
        结束时推 __onDone / __onSkip。mode 决定回答格式
        （normal/brief/algo/system，见 assistant.MODE_FORMATS）。
        """
        with self._stream_lock:
            self._stream_seq += 1
            sid = self._stream_seq

        def _worker():
            import time as _time
            from engine import store as store_mod
            qa_id = None
            t0 = _time.time()
            first_ms = None
            # 通知前端新建问答卡片（聚合器触发时前端没有卡片）
            _push("window.__onAsk&&window.__onAsk(%d,%s)" % (sid, _js_str(text)))
            try:
                # 在有活跃会话时创建 QA 记录（pending）
                if self._cur_sid is not None:
                    qa_id = store_mod.add_qa(self._db, self._cur_sid, text, mode=mode)

                def _on_chunk(piece, acc):
                    nonlocal first_ms
                    if first_ms is None and (piece or "").strip():
                        first_ms = int((_time.time() - t0) * 1000)
                    # 流式片段追加写入 answer（只追加不覆盖）
                    if qa_id is not None:
                        store_mod.append_qa_answer(self._db, qa_id, piece)
                    _push("window.__onChunk&&window.__onChunk(%d,%s,%s)"
                          % (sid, _js_str(piece), _js_str(acc)))

                full, contexts = self.assistant.ask_stream(
                    text, recent=recent, mode=mode, on_chunk=_on_chunk,
                )
                if (full or "").strip().startswith("[SKIP]"):
                    if qa_id is not None:
                        store_mod.finish_qa(self._db, qa_id, status="skip",
                                            first_token_ms=first_ms,
                                            total_ms=int((_time.time()-t0)*1000))
                    _push("window.__onSkip&&window.__onSkip(%d)" % sid)
                    return
                srcs = [{"title": c["title"], "text": (c["text"] or "")[:200]}
                        for c in (contexts or [])]
                if qa_id is not None:
                    store_mod.finish_qa(self._db, qa_id, status="done",
                                        first_token_ms=first_ms,
                                        total_ms=int((_time.time()-t0)*1000),
                                        sources_json=json.dumps(srcs, ensure_ascii=False))
                _push("window.__onDone&&window.__onDone(%d,%s,%s,%s)" % (
                    sid, _js_str(full), _js_str(json.dumps(srcs, ensure_ascii=False)),
                    _js_str(self.assistant.last_error or ""),
                ))
            except Exception as e:
                if qa_id is not None:
                    store_mod.finish_qa(self._db, qa_id, status="error",
                                        first_token_ms=first_ms,
                                        total_ms=int((_time.time()-t0)*1000))
                _push("window.__onError&&window.__onError(%d,%s)" % (sid, _js_str(str(e))))

        threading.Thread(target=_worker, daemon=True).start()
        return {"stream_id": sid, "llm": type(self.assistant.llm).__name__}

    def ask(self, text, recent=None, mode="normal"):
        """非流式问答（保留给内部/测试用；界面走 ask_stream）。"""
        try:
            answer, contexts = self.assistant.ask(text, recent=recent, mode=mode)
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

    # ================= 语音聚合器 =================
    def _on_aggregated_question(self, merged_text):
        """聚合器判定为问句且静默达标时回调：触发异步流式提问。

        走 ask_async 而非 ask_stream——新问题到来时自动取消上一条，
        部分答案保留为 interrupted。
        """
        from engine import store as store_mod
        qa_id = None
        if self._cur_sid is not None:
            qa_id = store_mod.add_qa(self._db, self._cur_sid, merged_text, mode=self._ai_mode)
        # 通知前端新建卡片
        with self._stream_lock:
            self._stream_seq += 1
            sid = self._stream_seq
        _push("window.__onAsk&&window.__onAsk(%d,%s)" % (sid, _js_str(merged_text)))
        self.ask_async(qa_id, merged_text, mode=self._ai_mode, recent=None)

    def agg_tick(self):
        """前端每 200ms 调一次：驱动麦克风聚合器 + 回环会议合并器。

        回环用 SilenceMerger（不做正则问句判定），判定交给两段式提示词。
        """
        try:
            self._aggregator.tick()
        except Exception:
            pass
        try:
            merger = getattr(self, "_loop_merger", None)
            if merger is not None:
                merger.tick()
        except Exception:
            pass
        return {"ok": True}

    # ================= 会话持久化 =================
    def start_session(self, title=""):
        """新建一次会话，返回 {"session_id": N}。
        后续 asr_feed 的定稿句和 ask_stream 的问答都挂到这个会话上。"""
        from engine import store as store_mod
        sid = store_mod.new_session(self._db, title=title)
        self._cur_sid = sid
        return {"session_id": sid}

    def load_history(self, session_id=None):
        """加载某次会话的转写片段和问答记录。
        session_id 不传则取最近一次。返回 {"session_id", "segments", "qa"}。"""
        from engine import store as store_mod
        if session_id is None:
            row = store_mod.latest_session(self._db)
            if row is None:
                return {"session_id": None, "segments": [], "qa": []}
            session_id = row["id"]
        segs = store_mod.list_segments(self._db, session_id)
        qas = store_mod.list_qa(self._db, session_id)
        return {"session_id": session_id, "segments": segs, "qa": qas}

    def get_practice_bank(self):
        """返回本机扩展练习题库（牛客 + 微信 + PDF/XMind），供“练习题库”页合并。

        题库只存在 %APPDATA%\\InterviewMonkey\\knowledge\\*_bank.json，
        不打包进 exe，也不进入项目仓库。文件不存在时返回空列表；
        某个文件损坏时跳过它并返回 errors，其他题库仍可用。
        """
        directory = os.path.join(data_dir(), "knowledge")
        sources = (
            ("nowcoder_bank.json", "Nowcoder", "牛客网"),
            ("wechat_bank.json", "微信资料", "微信资料"),
            ("study_bank.json", "资料", "资料"),
        )
        items = []
        seen = set()
        errors = []
        for filename, default_cat, default_from in sources:
            path = os.path.join(directory, filename)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                errors.append("%s: %s" % (filename, e))
                continue
            if isinstance(data, dict):
                data = data.get("items", [])
            if not isinstance(data, list):
                errors.append("%s: 题库格式错误" % filename)
                continue
            for row in data:
                if not isinstance(row, dict):
                    continue
                q = str(row.get("q") or "").strip()
                if not q:
                    continue
                key = "".join(q.split())
                if key in seen:
                    continue
                seen.add(key)
                ref = row.get("ref") or []
                if not isinstance(ref, list):
                    ref = [str(ref)]
                items.append({
                    "cat": str(row.get("cat") or default_cat),
                    "from": str(row.get("from") or default_from),
                    "q": q,
                    "ref": [str(item) for item in ref if str(item).strip()],
                })
        result = {"ok": True, "items": items, "count": len(items)}
        if errors:
            result["errors"] = errors
        return result

    def evaluate_answer(self, question, ref_points, answer):
        """面试陪练：回答评分 + 优化建议 + 参考答案。"""
        return self.assistant.evaluate_answer(question, ref_points or [], answer or "")

    def ai_status(self):
        from engine import asr as asr_mod
        st = self.assistant.status()
        st["asr_offline"] = asr_mod.available()
        return st

    def set_mode(self, mode):
        """P7：前端模式按钮 → 后端。回环链路的 mode_instruction 用它替换。

        mode ∈ normal/brief/detail/algo/system；未知值回落 normal。
        """
        valid = ("normal", "brief", "detail", "algo", "system")
        self._ai_mode = mode if mode in valid else "normal"
        _log("[P7] set_mode -> %s" % self._ai_mode)
        return {"ok": True, "mode": self._ai_mode}

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
        final 是一次性的：取走后置空。
        麦克风属于「我」：只落库 + 转写，不推聚合器——只有「对方」能触发自动问答。
        """
        import base64
        from engine import store as store_mod
        sess = getattr(self, "_asr_sess", None)
        if sess is None:
            return {"text": "", "final": None}
        try:
            text = sess.feed(base64.b64decode(b64_pcm))
            final, sess._pending = sess._pending, None
            if final:
                # 「我」说的话只留转写记录，不触发自动问答
                if self._cur_sid is not None:
                    store_mod.add_segment(self._db, self._cur_sid, final,
                                          speaker="我")
            return {"text": text, "final": final}
        except Exception:
            return {"text": "", "final": None}

    def asr_stop(self):
        """结束本次监听，返回 {"text": 全文, "final": 最后一句（可能为 None）}。
        最后一句定稿同样以 speaker="我" 落库，不推聚合器。"""
        from engine import store as store_mod
        sess = getattr(self, "_asr_sess", None)
        text, final = "", None
        try:
            if sess is not None:
                text = sess.finish()
                final, sess._pending = sess._pending, None
                if final:
                    if self._cur_sid is not None:
                        store_mod.add_segment(self._db, self._cur_sid, final,
                                              speaker="我")
        except Exception:
            text, final = "", None
        finally:
            self._asr_sess = None
        return {"text": text, "final": final}

    # ================= 回环采集：抓系统正在播放的声音 =================
    # 与麦克风链路并列：LoopbackCapture 的回调只入队，VAD + 解码全部在
    # 工作线程里跑（规格要求「不要在音频回调里调 feed」）。
    def list_sources(self):
        """列出 WASAPI 回环设备（系统声音），供前端音源下拉框使用。"""
        from engine import capture as cap_mod
        try:
            devs = cap_mod.list_loopback_devices()
        except Exception as e:
            return {"ok": False, "error": str(e), "sources": [], "names": []}
        return {
            "ok": True,
            "sources": devs,
            "names": [d["name"] for d in devs],
            "default": next((d["name"] for d in devs if d.get("is_default")), ""),
            "count": len(devs),
        }

    def loopback_status(self):
        """给界面看的依赖/模型状态：是否装了 pyaudiowpatch、用的是哪种识别。"""
        from engine import asr2
        try:
            st = asr2.status()
        except Exception as e:
            return {"ok": False, "error": str(e), "loopback": False}
        st["ok"] = bool(st.get("loopback"))
        st["running"] = getattr(self, "_loop_cap", None) is not None
        return st

    def _on_loopback_final(self, text):
        """一句回环定稿：识别 → 纠错 → 落库(segments, speaker=对方) → 推送前端。"""
        from engine import store as store_mod
        from engine import pipeline as pipe_mod
        text = (text or "").strip()
        if not text:
            return
        try:
            corrected = pipe_mod.apply_corrections(text)
        except Exception:
            corrected = text
        try:
            if self._cur_sid is not None:
                store_mod.add_segment(self._db, self._cur_sid, corrected,
                                      speaker="对方")
            merger = getattr(self, "_loop_merger", None)
            if merger is not None:
                merger.push(corrected)       # 判定交给两段式提示词，不用正则过滤
            else:
                self._aggregator.push_final(corrected)   # 兜底
        except Exception:
            pass
        _push("window.onLoopbackFinal&&window.onLoopbackFinal(%s,%s)"
              % (_js_str(corrected), _js_str("对方")))

    def _on_loop_question(self, merged_text):
        """回环静默达标：把「对方说的话」交给两段式提示词判定 + 整理 + 回答。

        P7 流式一体化：assistant.ask_extract_stream 边生成边解析——
        出现【无问题】立即取消、不建卡片（转写照常保留，已在 segments 里）；
        出现【回答】才建卡片：【问题】行做标题、「对方」原话进 raw_text 字段，
        之后答案增量逐段推前端。
        """
        import time as _time
        from engine import store as store_mod
        from engine import meeting_prompt as mp

        hist = list(getattr(self, "_loop_recent", []) or [])
        # 最近 6 句「对方」转写（旧→新，含本次合并句）+ 上一题问题文本
        recent = hist[-5:] + [merged_text]
        self._loop_recent = (hist + [merged_text])[-20:]
        try:
            ctxs = self.assistant._contexts(merged_text)
        except Exception:
            ctxs = []
        mode = self._ai_mode
        # 详细模式需要 500+ 字，默认 num_predict 装不下；按模式放宽
        num_predict = {"detail": 1500, "algo": 1200, "system": 1200}.get(mode, 700)
        prompt = mp.build(
            recent=recent,
            last_question=getattr(self, "_loop_last_question", None),
            ctx=ctxs,
            hotwords=getattr(self, "_loop_hotwords", "") or mp.load_hotwords(),
            mode=mode,
        )
        _log("[P7] mode=%s num_predict=%d recent=%d句\n[P7] prompt[:300]=%s"
             % (mode, num_predict, len(recent), prompt[:300]))

        def _worker():
            t0 = _time.time()
            state = {"qa_id": None, "first": None}

            def _on_question(question):
                """【回答】出现时建卡片；乱序晚到的重复事件只忽略（卡已建）。"""
                if state["qa_id"] is not None:
                    return
                state["qa_id"] = None
                try:
                    if self._cur_sid is not None:
                        state["qa_id"] = store_mod.add_qa(
                            self._db, self._cur_sid, question or merged_text,
                            mode=mode, raw_text=merged_text)
                except Exception:
                    state["qa_id"] = None
                self._loop_last_question = question or merged_text
                _push("window.onQuestion&&window.onQuestion(%d,%s)"
                      % (state["qa_id"] or 0, _js_str(question or merged_text)))

            def _on_delta(piece, _full):
                if state["qa_id"] is None:
                    _on_question(merged_text)   # 首个增量先建卡（标题用原话兜底）
                try:
                    store_mod.append_qa_answer(self._db, state["qa_id"], piece)
                except Exception:
                    pass
                if state["first"] is None:
                    state["first"] = int((_time.time() - t0) * 1000)
                _push("window.onAnswerDelta&&window.onAnswerDelta(%d,%s)"
                      % (state["qa_id"] or 0, _js_str(piece)))

            def _dispatch(ev, *args):
                if ev == "question":
                    _on_question(args[0])
                else:
                    _on_delta(*args)

            try:
                parsed = self.assistant.ask_extract_stream(
                    prompt, contexts=ctxs, on_event=_dispatch,
                    num_predict=num_predict)
            except Exception as e:
                parsed = {"has_question": True, "question": "",
                          "answer": "回答失败：%s" % e}
            if not parsed["has_question"]:
                return                      # 寒暄 / 陈述 / 闲聊：不建卡片，转写照常保留
            # 乱序提取出的整理问题，作为下一轮 {last_question}
            if parsed.get("question"):
                self._loop_last_question = parsed["question"]
            try:
                if state["qa_id"] is not None:
                    # 答案增量已在 _on_delta 里逐段落库，这里只收尾
                    store_mod.finish_qa(self._db, state["qa_id"], status="done",
                                        first_token_ms=state["first"],
                                        total_ms=int((_time.time() - t0) * 1000))
            except Exception:
                pass
            _push("window.onAnswerDone&&window.onAnswerDone(%d,%s)"
                  % (state["qa_id"] or 0,
                     _js_str(json.dumps({"status": "done"}, ensure_ascii=False))))

        threading.Thread(target=_worker, daemon=True).start()

    def start_capture(self, device_index=None):
        """启动系统声音采集 + 两遍识别（规范入口）。

        音频回调只入队，queue.Queue 由独立工作线程取出送入识别；
        模型缺失时自动用能量 VAD + 内置模型兜底。
        """
        import queue
        from engine import capture as cap_mod
        from engine import asr2
        from engine import store as store_mod
        if getattr(self, "_loop_cap", None) is not None:
            return {"ok": True, "already": True}
        if not cap_mod.available():
            return {"ok": False, "error": "缺少 pyaudiowpatch 或系统没有 WASAPI 回环设备"}

        if self._cur_sid is None:
            self._cur_sid = store_mod.new_session(self._db, "回环采集")

        from engine import meeting_prompt as mp
        # P7 触发条件放宽：静默 >=1.2s、合并文本 >=8 字才调 LLM（默认值）
        self._loop_merger = mp.SilenceMerger(self._on_loop_question)
        self._loop_recent = []
        self._loop_last_question = None
        self._loop_hotwords = mp.load_hotwords()

        q = queue.Queue(maxsize=200)
        stop_evt = threading.Event()

        def on_pcm(pcm16):
            # 音频回调：只入队。队列满了丢最旧的一块，宁可丢音频也不卡住播放。
            if q.full():
                try:
                    q.get_nowait()
                except Exception:
                    pass
            try:
                q.put_nowait(pcm16)
            except Exception:
                pass

        try:
            sess = asr2.build(self._on_loopback_final)
        except Exception as e:
            return {"ok": False, "error": "识别器初始化失败：%s" % e}

        def _worker():
            while not stop_evt.is_set():
                try:
                    pcm = q.get(timeout=0.2)
                except Exception:
                    continue
                try:
                    sess.feed(pcm)
                except Exception:
                    pass
            try:
                sess.flush()
            except Exception:
                pass

        cap = cap_mod.LoopbackCapture(on_pcm, device_index)
        try:
            cap.start()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        self._loop_cap, self._loop_q = cap, q
        self._loop_stop, self._loop_sess = stop_evt, sess
        threading.Thread(target=_worker, daemon=True).start()

        # 设备热插拔监听：插拔耳机 / 换默认输出时自动重开回环
        self._cap_on_pcm = on_pcm
        self._cap_device_index = device_index
        self._cap_device_sig = None
        self._cap_watch_stop = threading.Event()
        threading.Thread(target=self._watch_devices, daemon=True).start()
        return {"ok": True, "mode": asr2.status().get("mode", ""),
                "device": getattr(cap, "device_name", "") or ""}

    def stop_capture(self):
        """停止系统声音采集；尾部未断句的一段由工作线程 flush 出来。"""
        watch = getattr(self, "_cap_watch_stop", None)
        if watch is not None:
            watch.set()
        self._cap_watch_stop = None
        self._cap_on_pcm = None
        self._cap_device_index = None
        self._cap_device_sig = None
        cap = getattr(self, "_loop_cap", None)
        evt = getattr(self, "_loop_stop", None)
        if evt is not None:
            evt.set()
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass
        self._loop_cap = self._loop_q = None
        self._loop_stop = self._loop_sess = None
        self._loop_merger = None
        return {"ok": True}

    def _watch_devices(self):
        """每 2 秒查一次回环设备列表；插拔耳机/换默认输出时自动重开并提示界面。

        只替换采集对象，queue、工作线程、识别器和本监听线程都不重建，
        避免重启期间丢句子。
        """
        from engine import capture as cap_mod
        evt = self._cap_watch_stop
        while evt is not None and not evt.wait(2.0):
            try:
                devs = cap_mod.list_loopback_devices()
                sig = tuple((d["index"], d["name"]) for d in devs)
            except Exception:
                continue
            if self._cap_device_sig is None:
                self._cap_device_sig = sig
                continue
            if sig == self._cap_device_sig:
                continue
            self._cap_device_sig = sig
            default = next((d["name"] for d in devs if d.get("is_default")), "")
            try:
                old = self._loop_cap
                if old is not None:
                    old.stop()
                cap = cap_mod.LoopbackCapture(self._cap_on_pcm,
                                             self._cap_device_index)
                cap.start()
                self._loop_cap = cap
                note = "音频设备变化，已自动重连：" + (default or cap.device_name or "默认设备")
            except Exception as e:
                note = "音频设备变化，重连失败：%s" % e
            _push("window.onCaptureDeviceChanged&&window.onCaptureDeviceChanged(%s)"
                  % _js_str(note))

    # 兼容早期名字（旧前端 / 旧脚本可能还在调）
    def loopback_start(self, device_index=None):
        return self.start_capture(device_index)

    def loopback_stop(self):
        return self.stop_capture()

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

    def minimize_window(self):
        """最小化主窗口，供顶部快捷键调用。"""
        windows = webview.windows
        if not windows:
            return False
        windows[0].minimize()
        return True

    def _apply_window_maximized_state(self, maximized):
        """把真实窗口状态同步回页面，供自绘标题栏使用。"""
        self._window_maximized = bool(maximized)
        windows = webview.windows
        if not windows:
            return
        state = "true" if self._window_maximized else "false"
        icon = "'❐'" if self._window_maximized else "'□'"
        script = (
            "document.body.classList.toggle('maximized', %s);"
            "(()=>{const b=document.querySelector('#winMax');"
            "if(b)b.textContent=%s;})();" % (state, icon)
        )
        try:
            windows[0].evaluate_js(script)
        except Exception:
            pass

    def restore_window(self):
        """恢复主窗口，供顶部快捷键调用。"""
        windows = webview.windows
        if not windows:
            return False
        windows[0].restore()
        self._apply_window_maximized_state(False)
        return True

    def resize_window(self, width, height):
        """右下角拖拽缩放：JS 传入目标尺寸，这里做最小尺寸保护。"""
        windows = webview.windows
        if not windows:
            return {"ok": False}
        try:
            target_w = max(920, min(4096, int(float(width))))
            target_h = max(600, min(3072, int(float(height))))
            windows[0].resize(target_w, target_h)
            return {"ok": True, "width": target_w, "height": target_h}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def toggle_maximize_window(self):
        """最大化 / 还原无边框主窗口。"""
        windows = webview.windows
        if not windows:
            return {"ok": False, "maximized": False}
        window = windows[0]
        try:
            target = not self._window_maximized
            if target:
                window.maximize()
            else:
                window.restore()
            self._window_maximized = target
            return {"ok": True, "maximized": target}
        except Exception as exc:
            self._window_maximized = bool(getattr(window, "maximized", False))
            return {
                "ok": False,
                "maximized": self._window_maximized,
                "error": str(exc),
            }

    def close_window(self):
        """关闭主窗口；无边框窗口需要自绘关闭按钮。"""
        windows = webview.windows
        if not windows:
            return False
        windows[0].destroy()
        return True


class GlobalWindowHotkeys:
    """注册 Windows 全局快捷键，让最小化后也能恢复窗口。"""

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    HOTKEY_MINIMIZE = 1
    HOTKEY_RESTORE = 2

    def __init__(self, window, title, logger=_log):
        self.window = window
        self.title = title
        self.logger = logger
        self._thread_id = 0
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._message_loop,
            name="面试猴全局快捷键",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *_args, **_kwargs):
        if not self._thread_id:
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        except Exception as exc:
            self.logger("[hotkey] stop failed: %s" % exc)

    def _message_loop(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # 建立本线程消息队列
        self._thread_id = kernel32.GetCurrentThreadId()
        self.logger("[hotkey] message thread started id=%d" % self._thread_id)
        modifiers = self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT
        registrations = []
        for hotkey_id, virtual_key, label in (
            (self.HOTKEY_MINIMIZE, 0x28, "Ctrl+Shift+Down"),
            (self.HOTKEY_RESTORE, 0x26, "Ctrl+Shift+Up"),
        ):
            ctypes.set_last_error(0)
            if user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key):
                registrations.append(hotkey_id)
                self.logger("[hotkey] registered: %s" % label)
            else:
                self.logger("[hotkey] register failed: %s win32_error=%d" % (label, ctypes.get_last_error()))

        try:
            if not registrations:
                return
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if msg.message != self.WM_HOTKEY:
                    continue
                try:
                    if msg.wParam == self.HOTKEY_MINIMIZE:
                        self.window.minimize()
                    elif msg.wParam == self.HOTKEY_RESTORE:
                        self.window.restore()
                        hwnd = user32.FindWindowW(None, self.title)
                        if hwnd:
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            user32.SetForegroundWindow(hwnd)
                except Exception as exc:
                    self.logger("[hotkey] action failed id=%s error=%s" % (msg.wParam, exc))
        finally:
            for hotkey_id in registrations:
                user32.UnregisterHotKey(None, hotkey_id)
            self._thread_id = 0


def _show_fatal(title, message):
    """启动失败时尽量让用户看见原因：优先弹原生消息框，退化为打印。

    桌面应用最怕的就是「双击没反应」。窗口起不来时连浏览器都没有，
    只能走 Win32 MessageBox。
    """
    print("[面试猴] %s\n%s" % (title, message), flush=True)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def main():
    # WebView2 会继承系统代理，导致 127.0.0.1 也被代理转发、页面打不开；
    # 必须让浏览器进程绕过代理，才能用本地 http 提供服务（安全上下文）。
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--no-proxy-server")

    ok, detail = check_webview2()
    if not ok:
        _show_fatal(
            "面试猴 · 无法启动",
            "%s\n\n"
            "请安装或修复 Microsoft Edge WebView2 运行时：\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
            "若已安装仍报错，可尝试在「设置 - 应用」中修复 "
            "Microsoft Edge WebView2 Runtime。" % detail,
        )
        return 1

    html = resource_path(os.path.join("renderer", "index.html"))
    realtime_runtime = None
    realtime_url = ""
    try:
        from realtime_server import BackgroundRealtimeServer

        realtime_runtime = BackgroundRealtimeServer()
        realtime_url = realtime_runtime.start()
        _log("[realtime] embedded service: %s" % realtime_url)
    except Exception as e:
        _log("[realtime] embedded service failed: %s" % e)

    api = Api(realtime_url=realtime_url)
    api.preheat_asr()
    api.preheat_llm()
    window_title = "面试猴 · 会议实时总结助手"
    # 无边框透明窗口靠页面自己画标题栏；只让明确标注的拖拽区移动窗口，
    # 避免拖动滑块/按钮时误触发窗口移动。
    webview.settings["DRAG_REGION_SELECTOR"] = ".pywebview-drag-region"
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    main_window = webview.create_window(
        window_title,
        html,
        js_api=api,
        width=1160,
        height=780,
        min_size=(920, 600),
        background_color="#000000",
        transparent=True,
        frameless=True,
        easy_drag=False,
    )
    main_window.events.maximized += lambda: api._apply_window_maximized_state(True)
    main_window.events.restored += lambda: api._apply_window_maximized_state(False)
    window_hotkeys = GlobalWindowHotkeys(main_window, window_title, logger=_log)
    main_window.events.closed += window_hotkeys.stop
    window_hotkeys.start()
    # before_show 阶段先保护再显示；恢复、跨屏和句柄重建后由后台巡检重申。
    window_security = WindowSecurityGuard(window_title, logger=_log, audit_interval=2.0)
    window_security.bind_pywebview(main_window)
    # WebView2 顶层窗口没有可靠的原生逐像素透明；采样窗口背后的桌面作为
    # 页面背景，既去掉不透明黑底，也不会把应用自身拍进背景。
    from engine.desktop_backdrop import DesktopBackdrop

    desktop_backdrop = DesktopBackdrop(main_window, window_security, logger=_log)
    main_window.events.loaded += lambda: desktop_backdrop.start()
    main_window.events.closed += desktop_backdrop.stop
    # storage_path 让 localStorage 持久化，保证记录跨会话保留。
    # pywebview 6 默认用 http://127.0.0.1 提供页面（是安全上下文），麦克风因此可用；
    # 这里不指定端口，避免换端口导致 localStorage 源变化、历史记录"消失"。
    #
    # watchdog：个别环境（如 Windows 11 Build 26200 + pywebview 6.2.1）里
    # webview.start() 既不抛异常、窗口也不出现，表现为长时间静默无响应。
    # 这里加个兜底，15 秒还没加载完就明确告诉用户卡在哪一步。
    _watch_fired = {"v": False}

    def _watchdog():
        import time
        import webview as _wv
        for _ in range(150):          # 15s，每 100ms 查一次
            time.sleep(0.1)
            try:
                wins = _wv.windows
                if wins and wins[0].events.loaded.is_set():
                    return            # 正常起来了
            except Exception:
                return
        _watch_fired["v"] = True
        _show_fatal(
            "面试猴 · 窗口未能加载",
            "界面在 15 秒内没有加载出来。\n"
            "已检测到 WebView2 运行时：%s（本身正常）\n\n"
            "常见原因：pywebview 的 .NET 包装层与当前 Windows 版本不兼容。\n\n"
            "建议依次尝试：\n"
            "1. 在「设置 - 应用」中修复 Microsoft Edge WebView2 Runtime\n"
            "2. 安装 Windows 的最新累积更新\n"
            "3. 删除目录后重启（不会影响你的记录）：\n"
            "   %s\\EBWebView\\Default\\Cache\n"
            % (detail or "未知", data_dir()),
        )

    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        webview.start(storage_path=data_dir(), private_mode=False)
    except Exception as e:
        msg = str(e)
        hint = ""
        if "compatible Webview2 Runtime" in msg or "failed to start" in msg.lower():
            hint = (
                "\n\n这是 WebView2 运行时/包装层的兼容问题，不是配置问题。\n"
                "已检测到的运行时版本：%s\n"
                "可尝试：1) 修复 Microsoft Edge WebView2 Runtime；"
                "2) 更新 Windows；3) 改用 WebView2 Evergreen 独立安装包。" % (detail or "未知")
            )
        _show_fatal("面试猴 · 窗口启动失败", msg + hint)
        return 1
    finally:
        window_security.stop()
        if realtime_runtime is not None:
            realtime_runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
