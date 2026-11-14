# -*- coding: utf-8 -*-
"""拼音近音纠错（第 5 段）

ASR 把「交并比」听成「焦并比」、把「CIoU」听成「西欧U」是高频错误。
两层处理：
  1. mapping（corrections.json）：直接 str.replace 修字面错；
  2. 术语表（terms.txt，沿用 hotwords.txt 的中文词）：按同长度滑窗比对拼音，
     把同音误写改成正确术语。

pypinyin 缺失时自动退化成「只做第 1 层」，程序不崩（离线兜底）。
"""
import json
import os

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TERMS = os.path.join(_ENGINE_DIR, "terms.txt")
DEFAULT_MAP = os.path.join(_ENGINE_DIR, "corrections.json")


def pypinyin_available():
    try:
        import pypinyin  # noqa: F401
        return True
    except Exception:
        return False


def load_terms(path_terms, path_map):
    """返回 (术语拼音表, 字面映射表)。

    术语拼音表：{术语: 拼音元组}；只有纯中文术语才建索引（含英文的
    如 "CIoU"/"Focal Loss" 交给第 1 层 mapping 处理）。
    """
    mapping = {}
    try:
        with open(path_map, encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            mapping = {str(k): str(v) for k, v in obj.items()}
    except Exception:
        mapping = {}

    py = {}
    if not pypinyin_available():
        return py, mapping
    try:
        from pypinyin import lazy_pinyin
    except Exception:
        return py, mapping

    try:
        with open(path_terms, encoding="utf-8") as f:
            terms = [t.strip() for t in f if t.strip()]
    except Exception:
        terms = []
    for t in terms:
        if all("\u4e00" <= c <= "\u9fff" for c in t):
            try:
                py[t] = tuple(lazy_pinyin(t))
            except Exception:
                continue
    return py, mapping


def fix(text, py, mapping):
    """先做字面映射，再做同音近音替换；py 为空时退化为纯 str.replace。"""
    if not text:
        return text
    for wrong, right in (mapping or {}).items():
        if wrong:
            text = text.replace(wrong, right)
    if not py:
        return text
    try:
        from pypinyin import lazy_pinyin
    except Exception:
        return text

    for term, p in py.items():
        n, i = len(term), 0
        while i <= len(text) - n:
            w = text[i:i + n]
            if (w != term and all("\u4e00" <= c <= "\u9fff" for c in w)
                    and tuple(lazy_pinyin(w)) == p):
                text = text[:i] + term + text[i + n:]
                i += n
            else:
                i += 1
    return text


def load_default():
    """便捷入口：加载项目自带的术语表 + 纠错表。"""
    return load_terms(DEFAULT_TERMS, DEFAULT_MAP)
