"""对话窗口、引用 Prompt 与 token 预算。"""
from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..contracts import Chunk, Transcript, TranscriptKind


_TOKEN = re.compile(r"[A-Za-z0-9_+.#/-]+|[\u4e00-\u9fff]")


def approximate_tokens(text: str) -> int:
    return len(_TOKEN.findall(text or ""))


class ConversationContext:
    def __init__(self, max_turns: int = 12):
        self.turns: deque[Transcript] = deque(maxlen=max_turns * 2)
        self.summary = ""

    def append(self, transcript: Transcript) -> None:
        self.turns.append(transcript)

    def recent(self, limit: int = 6) -> list[Transcript]:
        return list(self.turns)[-limit:]

    def compact(self, keep_recent: int = 6) -> None:
        old = list(self.turns)[:-keep_recent]
        if not old:
            return
        lines = [f"{item.speaker_id or item.channel.value}: {item.text.strip()}" for item in old if item.text.strip()]
        if lines:
            self.summary = (self.summary + "\n" + "；".join(lines)).strip()[-2000:]
        self.turns = deque(list(self.turns)[-keep_recent:], maxlen=self.turns.maxlen)


class PromptContextManager:
    def __init__(self, system_prompt_path: str | Path, conversation: ConversationContext | None = None):
        self.system_prompt = Path(system_prompt_path).read_text(encoding="utf-8").strip()
        self.conversation = conversation or ConversationContext()

    @staticmethod
    def _references(chunks: Sequence[Chunk]) -> str:
        if not chunks:
            return "【参考资料】\n（知识库中未找到达到置信度阈值的资料）"
        blocks = []
        for chunk in chunks:
            section = f" / {chunk.section}" if chunk.section else ""
            blocks.append(f"[来源: {chunk.id}] {chunk.source}{section}\n{chunk.text}")
        return "【参考资料】\n" + "\n\n".join(blocks)

    def build(
        self,
        query: str,
        retrieved: Sequence[Chunk],
        budget_tokens: int = 6000,
        *,
        glossary_candidates: Sequence[Mapping[str, object]] = (),
    ) -> list[Mapping[str, str]]:
        if approximate_tokens(self.system_prompt) >= budget_tokens:
            raise ValueError("token 预算小于固定系统提示词，不能裁剪系统指令")

        chunks = list(retrieved)
        self.conversation.compact(keep_recent=6)
        recent = self.conversation.recent(6)
        summary = self.conversation.summary

        def compose() -> list[Mapping[str, str]]:
            history = "\n".join(
                f"{item.speaker_id or item.channel.value}: {item.text.strip()}"
                for item in recent if item.kind is TranscriptKind.FINAL and item.text.strip()
            )
            dynamic = [self._references(chunks)]
            if summary:
                dynamic.append("【近期对话摘要】\n" + summary)
            if history:
                dynamic.append("【最近原文】\n" + history)
            if glossary_candidates:
                lines = ["【ASR 词库候选（仅供纠错推断，分数低不代表已确认）】"]
                for candidate in glossary_candidates[:3]:
                    raw = str(candidate.get("raw", "")).strip()
                    corrected = str(candidate.get("corrected", "")).strip()
                    if not raw or not corrected:
                        continue
                    score = candidate.get("score")
                    score_text = f"，匹配分数 {float(score):.3f}" if isinstance(score, (int, float)) else ""
                    lines.append(f"- {raw} → {corrected}{score_text}")
                if len(lines) > 1:
                    dynamic.append("\n".join(lines))
            dynamic.append("【当前问题】\n" + query)
            return [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": "\n\n".join(dynamic)},
            ]

        messages = compose()
        # 预算超限时先压缩原始历史，再缩短滚动摘要；检索片段优先级更高。
        while recent and sum(approximate_tokens(message["content"]) for message in messages) > budget_tokens:
            recent.pop(0)
            messages = compose()
        while summary and sum(approximate_tokens(message["content"]) for message in messages) > budget_tokens:
            summary = summary[max(1, len(summary) // 4):]
            messages = compose()
        while chunks and sum(approximate_tokens(message["content"]) for message in messages) > budget_tokens:
            chunks.pop()  # 最后才裁剪最低相关检索片段
            messages = compose()
        if sum(approximate_tokens(message["content"]) for message in messages) > budget_tokens:
            raise ValueError("当前问题与固定系统提示超过 token 预算")
        return messages
