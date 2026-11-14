# -*- coding: utf-8 -*-
"""第 4 段：语音转写聚合器 + 问句判定（Pipeline）

ASR 每 endpoint 产出一句定稿文本，但用户说话会跨多句断断续续——
比如"这是一个什么""是交并比"其实是同一个问题，不能分成两次提问。
QuestionAggregator 把短时间内的定稿句缓冲起来，静默超过阈值才合并成
一个问题一次性发给 LLM，避免把一句话拆成多次调用。

问句判定用关键词正则——非问句（"好的""嗯"）只留在转写里，不调 LLM。
纠错表 corrections.json 做 str.replace 修正常见 ASR 误识别（如"焦并比"→"交并比"）。
"""
import os
import re
import json
import time

# ---- 问句关键词正则（P7 按「放宽触发」要求补充触发词）----
Q_PAT = re.compile(r"(什么|怎么|如何|为什么|为何|区别|原理|是否|能否|吗|呢|哪|多少|介绍一下|讲讲|说一下|解释|谈谈|对比|优缺点|请|你觉得|有没有|是不是|能不能|可不可以)")


def looks_like_question(t):
    """判断一段文本是否像一个面试问题：长度 >=8 且命中问句关键词。"""
    return len(t) >= 8 and bool(Q_PAT.search(t))


class QuestionAggregator:
    """聚合短时间内的定稿句，静默超过阈值后合并成一个问题。

    on_question(merged_text) 在判定为问句且静默达标时被调用；
    非问句只留在 buf 里被清空，不触发回调。
    """

    def __init__(self, on_question, quiet_q=1.2, quiet_any=2.5, now=time.monotonic):
        self.buf, self.last, self.now = [], 0.0, now
        self.on_question, self.quiet_q, self.quiet_any = on_question, quiet_q, quiet_any

    def push_final(self, text):
        """推入一句定稿文本（非空才进缓冲），刷新最后活动时间。"""
        if text.strip():
            self.buf.append(text.strip())
            self.last = self.now()

    def tick(self):
        """每 200ms 调一次：检查静默时长，达标则合并并判定。"""
        if not self.buf:
            return
        quiet, merged = self.now() - self.last, "".join(self.buf)
        isq = looks_like_question(merged)
        if (isq and quiet >= self.quiet_q) or quiet >= self.quiet_any:
            self.buf.clear()
            if isq:
                self.on_question(merged)   # 非问句只留在转写里，不调 LLM


# ---- ASR 纠错表 ----
_CORRECTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corrections.json")
_corrections = None   # 懒加载


def _load_corrections():
    """懒加载 corrections.json，返回 {错: 对} 字典。文件不存在则空字典。"""
    global _corrections
    if _corrections is not None:
        return _corrections
    try:
        with open(_CORRECTIONS_PATH, "r", encoding="utf-8") as f:
            _corrections = json.load(f)
    except Exception:
        _corrections = {}
    if not isinstance(_corrections, dict):
        _corrections = {}
    return _corrections


_correct_state = None


def _load_correct_state():
    """懒加载「术语拼音表 + 字面映射表」，只解析一次。"""
    global _correct_state
    if _correct_state is None:
        from . import correct as correct_mod
        _correct_state = correct_mod.load_default()
    return _correct_state


def apply_corrections(text):
    """定稿文本进入聚合器前纠错：字面映射 + 拼音近音替换。

    correct.fix 在 pypinyin 不可用时会退化为原来的纯 str.replace，
    行为与旧版一致；任何异常也退回旧实现，绝不让转写链路中断。
    """
    if not text:
        return text
    try:
        from . import correct as correct_mod
        py, mapping = _load_correct_state()
        if not py and not mapping:
            return text
        return correct_mod.fix(text, py, mapping)
    except Exception:
        corr = _load_corrections()
        out = text
        for wrong, right in corr.items():
            out = out.replace(wrong, right)
        return out


# ---- skip 兜底：回答文本若匹配则一律不显示 ----
SKIP_RE = re.compile(r"\W*\[?skip\]?\W*", re.I)


def is_skip_text(s):
    """判断回答文本是否为纯 skip 标记（不显示）。"""
    return bool(SKIP_RE.fullmatch(s or ""))
