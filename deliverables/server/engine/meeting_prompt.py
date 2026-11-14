# -*- coding: utf-8 -*-
"""会议/电话陪练提示词：把「对方」说的话判定成问题、整理、再作答。

只服务于回环采集（系统声音）链路；麦克风与手动提问仍走原来的
assistant._prompt，互不干扰。

模板里的 {recent} 承载「对方最近说的话」（旧→新），模型负责：
  - 先判断对方是否在向「我」提问或布置任务；
  - 合并被拆开的句子、纠正同音错字、补全「那这个呢/为什么」这类指代；
  - 无提问时只输出【无问题】；
  - 有提问时严格按【问题】/【回答】两段输出。
"""
import os
import time

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
HOTWORDS_PATH = os.path.join(_ENGINE_DIR, "hotwords.txt")

# 「对方」的原话由 {recent} 传入；这里一字不改地保留规格模板。
PROMPT_TEMPLATE = r'''你是「面试猴」面试陪练助手。下面是电话/会议里「对方」最近说的话，来自语音识别，
可能有错字、被拆成多句。请先判断对方是否在向"我"提问或布置任务。

【输出格式】必须严格按下面两段输出：
【问题】用一句完整、通顺的话，写出对方真正想问的问题。
        结合【最近对话】合并被拆开的句子、纠正同音错字、补全"那这个呢/为什么"这类指代。
        如果对方没有提问（寒暄、陈述、闲聊），只输出：【无问题】，然后立刻结束，不要写别的。
【回答】{mode_instruction}

知识库相关就以知识库为准；无关就直接用专业知识回答，不要说"不知道"。
术语以这个词表为准：{hotwords}

【最近对话】（旧→新）
{recent}
【上一个问题】{last_question}
【知识库】{ctx}'''

MODE_INSTRUCTIONS = {
    "brief": "用一句话给出结论（60 字内），不要展开。",
    "normal": "先给结论，再展开 3 个要点，控制在 120 字内。",
    "detail": (
        "完整展开，用 Markdown 输出，总字数不少于 500 字，"
        "必须包含以下 5 个小节，每个小节都以「## 」开头的标题行：\n"
        "## 核心结论\n## 原理拆解\n## 具体例子\n## 易错点\n## 面试官追问\n"
        "小节内容多给干货和具体数字/公式，条理清晰，拒绝空话。"
    ),
    "algo": "按算法题答法：金句结论 → 思路与伪代码 → 复杂度 → 易错点。",
    "system": "按系统设计答法：选型权衡 → 架构与数据流/容量估算 → 瓶颈与追问。",
}


def mode_instruction(mode):
    """把中栏 5 个模式映射成 {mode_instruction}；未知模式退回普通。"""
    return MODE_INSTRUCTIONS.get(mode) or MODE_INSTRUCTIONS["normal"]


def load_hotwords(path=None):
    """读术语词表，返回用「、」连接的一行文本。"""
    try:
        with open(path or HOTWORDS_PATH, encoding="utf-8") as f:
            words = [w.strip() for w in f if w.strip()]
    except Exception:
        words = []
    return "、".join(words) if words else "（无词表）"


def format_recent(lines):
    """把最近几句格式化成「旧→新」的带序号文本。"""
    items = [str(x).strip() for x in (lines or []) if str(x).strip()]
    if not items:
        return "（无）"
    return "\n".join("%d. %s" % (i, t) for i, t in enumerate(items, 1))


def format_ctx(contexts):
    """知识库条目格式化；空列表给一个明确的占位说明。"""
    blocks = ["[%s] %s" % (c.get("title", "知识库"), c.get("text", ""))
              for c in (contexts or []) if c]
    return "\n\n".join(blocks) if blocks else "（无检索内容）"


def build(recent, last_question, ctx, hotwords, mode="normal"):
    """填充全部占位符，返回可直接送进 LLM 的 prompt。

    recent 可以是字符串列表（旧→新）或已格式化好的字符串；
    ctx 可以是知识库条目列表或已格式化好的字符串。
    """
    recent_text = recent if isinstance(recent, str) else format_recent(recent)
    ctx_text = ctx if isinstance(ctx, str) else format_ctx(ctx)
    return (PROMPT_TEMPLATE
            .replace("{mode_instruction}", mode_instruction(mode))
            .replace("{hotwords}", hotwords if isinstance(hotwords, str)
                     else "、".join(hotwords or []))
            .replace("{recent}", recent_text)
            .replace("{last_question}", (last_question or "").strip() or "（无）")
            .replace("{ctx}", ctx_text))


def parse(output):
    """解析两段式输出。

    返回 {"has_question", "question", "answer"}：
      - 出现【无问题】或空输出 -> has_question=False（跳过，不进左栏也不回答）
      - 正常两段 -> 拆出整理后的问题与回答
      - 模型没按格式输出 -> 把全文当回答，question 留空由调用方用原话兜底
    """
    text = (output or "").strip()
    empty = {"has_question": False, "question": "", "answer": ""}
    if not text or "【无问题】" in text:
        return empty
    q_at, a_at = text.find("【问题】"), text.find("【回答】")
    if q_at >= 0 and a_at > q_at:
        question = text[q_at + len("【问题】"):a_at].strip()
        answer = text[a_at + len("【回答】"):].strip()
        if not question and not answer:
            return empty
        return {"has_question": True, "question": question, "answer": answer}
    if a_at >= 0 and q_at > a_at:
        # 小模型偶尔乱序：先【回答】后【问题】——回答在标记之前，问题在标记之后
        question = text[q_at + len("【问题】"):].strip()
        answer = text[:a_at].strip()
        return {"has_question": True, "question": question, "answer": answer}
    return {"has_question": True, "question": "", "answer": text}


class SilenceMerger:
    """只按静默把被拆开的句子合并，不做正则问句判定。

    两段式提示词自己负责判断「对方是否在向我提问」，所以这里不能像
    QuestionAggregator 那样把非问句直接丢掉——寒暄、陈述也要送给模型，
    由它输出【无问题】。合并后整段作为 {recent} 的最后一句传入。

    P7 触发条件放宽：静默 >=1.2 秒即触发；合并文本 >=8 个字才调用 LLM
    （太短的碎片继续留在缓冲里，等后续句子拼起来再判定，不丢弃）。
    """

    def __init__(self, on_merged, quiet=1.2, min_chars=8, now=None):
        self.buf, self.last, self.now = [], 0.0, now or time.monotonic
        self.on_merged, self.quiet = on_merged, quiet
        self.min_chars = max(0, int(min_chars))

    def push(self, text):
        if text and str(text).strip():
            self.buf.append(str(text).strip())
            self.last = self.now()

    def tick(self):
        if not self.buf or self.now() - self.last < self.quiet:
            return
        merged = "".join(self.buf)
        if self.min_chars and len(merged) < self.min_chars:
            return          # 碎片太短：不触发也不丢弃，等下一句拼起来
        self.buf.clear()
        if merged:
            self.on_merged(merged)
