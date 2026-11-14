# -*- coding: utf-8 -*-
"""第 5 段：编排（Assistant）

把五段串起来：
    audio -> asr -> rag -> llm -> answer

同时提供降级路径：没有麦克风/本地 ASR 时，直接接受文本提问。
"""
from . import audio as audio_mod
from . import asr as asr_mod
from . import rag as rag_mod
from . import llm as llm_mod


class Assistant:
    def __init__(self, knowledge_dir=None, asr_engine="sherpa", asr_model=None,
                 llm_backend=None, bank_path=None, top_k=3):
        self.retriever = rag_mod.Retriever(knowledge_dir)
        self.asr = asr_mod.build_asr(asr_engine, asr_model) if asr_model else None
        self.llm = llm_backend or llm_mod.build_llm()
        # 5000 题库作为第二检索源：命中同类题的标准要点，降低小模型胡编的概率
        self.bank = rag_mod.load_bank(bank_path) if bank_path else None
        self.last_error = ""   # 最近一次大模型调用失败的原因（界面据此提示）
        self._ollama_fb = None  # 欠费/断网时的本地兜底后端（惰性构建）
        try:
            self.top_k = max(1, min(8, int(top_k)))
        except (TypeError, ValueError):
            self.top_k = 3

    def _fallback_backend(self):
        """在线后端挂了时的本地兜底：已装 Ollama 且有模型才返回，否则 None。"""
        if self._ollama_fb is not None:
            return self._ollama_fb
        try:
            if llm_mod.ollama_available() and llm_mod.ollama_models():
                self._ollama_fb = llm_mod.OllamaBackend(None)  # 自动挑已装最小模型
                return self._ollama_fb
        except Exception:
            self._ollama_fb = None
        return None

    def status(self):
        return {
            "audio": audio_mod.available(),
            "local_asr": self.asr is not None,
            "ollama": llm_mod.ollama_available(),
            "ollama_models": llm_mod.ollama_models(),
            "llm": type(self.llm).__name__,
            "llm_label": llm_mod.describe(self.llm),
            "knowledge_chunks": len(self.retriever.index.chunks),
            "bank_chunks": len(self.bank.chunks) if self.bank is not None else 0,
            "last_error": self.last_error,
        }

    def _contexts(self, question):
        """产品知识库 + 题库双路检索。top_k 来自「模型设置」，可调。"""
        ctxs = list(self.retriever.retrieve(question, top_k=self.top_k))
        if self.bank is not None and self.bank.chunks:
            for c in self.bank.search(question, 2):
                item = dict(c)
                item["title"] = item.get("title", "题库") + "·参考要点"
                ctxs.append(item)
        return ctxs

    def _prompt(self, question, contexts, recent=None):
        ctx = "\n\n".join(f"[{c['title']}] {c['text']}" for c in contexts) or "（无检索内容）"
        recent_text = "\n".join((recent or [])[-3:]) or "（无）"
        return (
            "你是「面试猴」实时问答助手，用户在练习面试或开会时，会不断说出一句话，"
            "你判断并作答。\n\n"
            "【输入说明】\n"
            "- 【最近对话】是前几句话，只用来理解指代（如“它”“这个”），不要回答它们。\n"
            "- 【当前这句】来自语音识别，可能有同音错字，请按上下文纠正\n"
            "  （例：西欧U→CIoU，焦并比→交并比，非及大值抑制→非极大值抑制）。\n\n"
            "【判断规则】\n"
            "1. 如果【当前这句】不是问题、不是请求解释的内容（寒暄、陈述、口头语），\n"
            "   只输出 [SKIP]，不要输出其他任何字。\n"
            "2. 如果是问题，按下面格式回答。\n\n"
            "【回答格式】（总共不超过 120 字；第一行必须最先、极短，用户盯着屏幕等它蹦出来）\n"
            "第一行：3 个极简关键词，用「、」隔开，总共不超过 15 个字，\n"
            "  不要加任何前缀、序号或标点开头——这一行就是界面上最先闪现的内容。\n"
            "空一行后展开：最多 3 个要点，每条不超过 25 字，用「·」开头。\n"
            "有公式或关键数字时给出，没有就不要凑。\n"
            "不要寒暄，不要复述问题，不要说“根据知识库”。\n\n"
            "【知识库使用】\n"
            "- 知识库与问题相关时，以知识库为准，不编造知识库以外的产品细节。\n"
            "- 知识库无关时，直接用你的专业知识回答，不要说“不知道”。\n\n"
            "【最近对话】\n" + recent_text + "\n\n"
            "【知识库】\n" + ctx + "\n\n"
            "【当前这句】\n" + question
        )

    def _call_backend_stream(self, backend, prompt, contexts):
        """统一拿到某个后端的流式迭代器；没有流式接口的老后端退回非流式。"""
        try:
            return backend.generate_stream(prompt, contexts)
        except TypeError:
            return backend.generate_stream(prompt)

    def _iter_llm_stream(self, prompt, contexts):
        """主后端流式产出，失败自动降级：本地 Ollama -> 规则兜底。

        yield (piece, source)，source ∈ {"main", "fallback", "rule", "note"}；
        note 是给人看的说明行（如「已切到本地重答」）。
        """
        main_err = None
        yielded_main = False
        try:
            for piece in self._call_backend_stream(self.llm, prompt, contexts):
                yielded_main = True
                yield piece, "main"
            return
        except Exception as e:
            main_err = e
            self.last_error = str(e)

        # 本地 Ollama 兜底（主后端不是 Ollama 时才降级）
        fb = None
        if not isinstance(self.llm, llm_mod.OllamaBackend):
            fb = self._fallback_backend()
        if fb is not None:
            try:
                if yielded_main:
                    yield "\n⚠️ 在线模型中断，已切本地重答：\n", "note"
                got = False
                for piece in self._call_backend_stream(fb, prompt, contexts):
                    got = True
                    yield piece, "fallback"
                if got:
                    self.last_error = ("在线模型不可用（%s），已临时切到本地 Ollama"
                                       % str(main_err)[:40])
                    return
            except Exception:
                pass

        # 规则兜底（最后防线）
        hint = ""
        if isinstance(self.llm, llm_mod.OpenAICompatBackend):
            hint = "可到「模型设置」核对服务商与 Key，或改用本地 Ollama。"
        elif isinstance(self.llm, llm_mod.OllamaBackend):
            hint = "可到「模型设置」选择已安装的本地模型。"
        yield "⚠️ 大模型调用失败：%s%s\n\n" % (main_err, ("\n" + hint) if hint else ""), "note"
        for piece in llm_mod.RuleBasedBackend().generate_stream(prompt, contexts):
            yield piece, "rule"

    def ask_stream(self, question, recent=None, on_chunk=None):
        """流式版核心链路：检索 -> 推理（边出边回调）。

        on_chunk(piece, full_text)：每收到一段就回调一次，界面做打字机渲染；
        on_chunk 传 None 时不回调，聚合为完整答案返回（行为等同旧 ask）。

        返回 (full_answer, contexts)。answer 为 "[SKIP]" 表示这句话不是问题。
        """
        question = (question or "").strip()
        if not question:
            return "（问题为空）", []
        # 短句（"它有什么缺点"）单独检索效果差，拼上前一两句一起检索
        retrieval_q = question
        if recent and len(question) < 10:
            retrieval_q = "；".join([*list(recent)[-2:], question])
        contexts = self._contexts(retrieval_q)
        if recent is not None and not contexts and isinstance(self.llm, llm_mod.RuleBasedBackend):
            return "[SKIP]", contexts
        prompt = self._prompt(question, contexts, recent)

        parts = []

        def emit(piece):
            parts.append(piece)
            if on_chunk:
                on_chunk(piece, "".join(parts))

        # ---- [SKIP] 流式判定：缓冲前几个字，确认不是 "[SKIP]" 才往界面吐 ----
        # 只缓冲到能判定为止（最多 6 个字符），几乎不影响首字延迟
        buf = ""
        decided = False

        for piece, source in self._iter_llm_stream(prompt, contexts):
            if source == "rule" and recent is not None:
                continue          # 自动模式：规则兜底不算回答，静默跳过
            if source == "note" and recent is not None and not "".join(parts).strip():
                continue              # 自动模式下开头的错误说明不刷屏，留在 last_error 里
            if not decided:
                buf += piece
                s = buf.lstrip()
                if not s:
                    continue
                if s.startswith("[SKIP]"):
                    decided = True
                    continue
                if len(s) >= 6 or not "[SKIP]".startswith(s):
                    decided = True
                    emit(buf)
                continue
            emit(piece)

        text = "".join(parts).strip()
        if text.startswith("[SKIP]"):
            return "[SKIP]", contexts
        # 自动模式下 profile 是「没给出任何有效回答」：静默跳过，别弹空气泡
        if recent is not None and not text:
            return "[SKIP]", contexts
        if not text:
            text = "（模型没有返回内容）"
        return text, contexts

    def ask(self, question, recent=None):
        """核心：文本问题 -> 检索 -> 推理 -> 答案。返回 (answer, contexts)。

        兼容入口：内部直接走 ask_stream 的流式链路再聚合，
        降级行为（在线挂->本地 Ollama->规则兜底）与旧版完全一致。
        """
        return self.ask_stream(question, recent, None)


    def ask_from_mic(self, duration=5.0):
        """麦克风 -> ASR -> 检索 -> 推理。"""
        if self.asr is None:
            return "（未配置本地 ASR，请直接用文本提问，或在 App 内用内置语音识别）", []
        pcm, sr = audio_mod.record_seconds(duration)
        if pcm is None:
            return "（麦克风不可用，请改用文本提问）", []
        text = self.asr.transcribe(pcm, sr)
        return self.ask(text)

    def evaluate_answer(self, question, ref_points, answer):
        """面试陪练：对用户回答打分并给优化建议。

        规则化打分始终可用；装了 Ollama / 配置了 OpenAI 兼容 API 时，
        额外生成一段完整的「参考答案」。
        """
        from . import evaluate as ev
        result = ev.rule_based_evaluate(question, ref_points, answer)
        result["reference"] = None
        result["llm"] = False
        # 仅在真实 LLM 后端可用时增强（规则兜底不适合生成参考答案）
        if isinstance(self.llm, (llm_mod.OllamaBackend, llm_mod.OpenAICompatBackend)):
            ref = ev.llm_reference(self.llm, question, ref_points)
            if ref:
                result["reference"] = ref
                result["llm"] = True
        return result
