"""句尾语义判断，共用于 VAD 延时与相邻 final 合并。"""
from __future__ import annotations

import re


_TRAILING_PUNCTUATION = re.compile(r"[。！？!?，,、；;：:…]+$")
_INCOMPLETE_END = re.compile(
    r"(?:的|和|与|是|那个|就是|但|但是|因为|所以|如果|然后|以及|由于|其中|把|被|在|对|或|及)$"
)
_TERMINAL_PARTICLE = re.compile(r"(?:吗|呢|啊|呀|吧|嘛|呐|啦)$")
_SENTENCE_PUNCTUATION = re.compile(r"[。！？!?]$")
_COMPLETE_REQUEST = re.compile(
    r"^(?:(?:请|麻烦)?(?:告诉我|解释(?:一下)?|介绍(?:一下)?|说一下|讲讲|分析(?:一下)?|对比|实现|说明(?:一下)?)).+$"
)


def is_incomplete_ending(text: str) -> bool:
    """识别需要继续等待的连接词/结构助词结尾，忽略末尾标点。"""
    value = re.sub(r"\s+", "", (text or "").strip())
    if value.endswith(("？", "?", "！", "!")):
        return False
    value = _TRAILING_PUNCTUATION.sub("", value)
    return bool(_INCOMPLETE_END.search(value))


def should_merge_after(text: str) -> bool:
    """未完结句子允许短窗口合并；完整指令请求直接收句，避免粘入后续噪声。"""
    value = re.sub(r"\s+", "", (text or "").strip())
    if not value:
        return False
    if is_incomplete_ending(value):
        return True
    if _COMPLETE_REQUEST.match(value):
        return False
    return not (_SENTENCE_PUNCTUATION.search(value) or _TERMINAL_PARTICLE.search(value))
