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
                 llm_backend=None, bank_path=None):
        self.retriever = rag_mod.Retriever(knowledge_dir)
        self.asr = asr_mod.build_asr(asr_engine, asr_model) if asr_model else None
        self.llm = llm_backend or llm_mod.build_llm()
        # 5000 题库作为第二检索源：命中同类题的标准要点，降低小模型胡编的概率
        self.bank = rag_mod.load_bank(bank_path) if bank_path else None

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
        }

    def _contexts(self, question):
        """产品知识库 + 题库双路检索。"""
        ctxs = list(self.retriever.retrieve(question))
        if self.bank is not None and self.bank.chunks:
            for c in self.bank.search(question, 2):
                item = dict(c)
                item["title"] = item.get("title", "题库") + "·参考要点"
                ctxs.append(item)
        return ctxs

    def _prompt(self, question, contexts):
        ctx = "\n\n".join(f"[{c['title']}] {c['text']}" for c in contexts) or "（无检索内容）"
        return (
            "你是「面试猴」面试陪练助手，服务视觉 / 深度学习 / 3D 视觉方向的算法工程师。\n"
            "答题规则：\n"
            "1) 若【知识库】与问题相关，优先依据知识库作答，不要编造知识库以外的产品细节；\n"
            "2) 若知识库与问题无关（例如算法面试题），直接用你的专业知识作答，"
            "给出定义、关键公式/实现思路和常见追问点，不要因为知识库没有就说不知道；\n"
            "3) 中文、口语化、条理清晰，控制在 200 字内，面试题优先给可背诵的要点。\n\n"
            "【知识库】\n" + ctx + "\n\n【用户问题】\n" + question
        )

    def ask(self, question):
        """核心：文本问题 -> 检索 -> 推理 -> 答案。返回 (answer, contexts)。

        大模型调用失败时不把异常甩给界面：自动降级到本地知识库兜底，
        并在回答开头说明原因，保证用户始终拿到结果。
        """
        question = (question or "").strip()
        if not question:
            return "（问题为空）", []
        contexts = self._contexts(question)
        prompt = self._prompt(question, contexts)
        # RuleBasedBackend 接受 contexts 参数，其余后端忽略
        try:
            try:
                answer = self.llm.generate(prompt, contexts)
            except TypeError:
                answer = self.llm.generate(prompt)
        except Exception as e:
            fb = llm_mod.RuleBasedBackend()
            body = fb.generate(prompt, contexts)
            hint = ""
            if isinstance(self.llm, llm_mod.OpenAICompatBackend):
                hint = "可到「模型设置」核对服务商与 Key，或改用本地 Ollama。"
            elif isinstance(self.llm, llm_mod.OllamaBackend):
                hint = "可到「模型设置」选择已安装的本地模型。"
            answer = "⚠️ 大模型调用失败：%s%s\n\n%s" % (e, ("\n" + hint) if hint else "", body)
            return answer, contexts
        return answer, contexts


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
