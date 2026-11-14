"""声道/说话人门控、final 合并和问题检测。"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from ..contracts import Channel, QuestionDecision, Transcript, TranscriptKind
from ..utterance import is_incomplete_ending, should_merge_after


_QUESTION_WORDS = re.compile(
    r"(什么|怎样|怎么|如何|为什么|为何|哪(?:个|些|里)?|多少|是否|能否|吗|呢|区别|原理|"
    r"优缺点|是不是|能不能|可不可以|有没有)"
)
_COMMAND = re.compile(r"^(请|麻烦)?\s*(告诉我|解释|介绍|说一下|讲讲|分析|对比|实现|说明)")
_SMALL_TALK = re.compile(r"^(你好|您好|谢谢|好的|好吧|嗯+|哦+|知道了|没问题|再见)[啊呀呢吧。！!]*$")
_PRONOUN = re.compile(r"^(它|这个|那个|那这个|这一个|为什么|那为什么|它的|这个的)")


def _combine_final_transcripts(previous: Transcript, current: Transcript) -> Transcript:
    """合并相邻 final，同时保留 ASR/纠错追踪字段。"""
    return Transcript(
        TranscriptKind.FINAL,
        previous.text.rstrip("，,。！？!? ") + "，" + current.text.lstrip(),
        current.channel,
        previous.start_ms,
        current.end_ms,
        current.speaker_id or previous.speaker_id,
        min(previous.confidence or 1.0, current.confidence or 1.0),
        current.trace_id or previous.trace_id,
        utterance_id="+".join(filter(None, (
            previous.utterance_id, current.utterance_id,
        ))) or None,
        raw_text="；".join(filter(None, (
            previous.raw_text or previous.text,
            current.raw_text or current.text,
        ))) or None,
        corrected=previous.corrected or current.corrected,
        asr_engine=current.asr_engine or previous.asr_engine,
        pass1_ms=sum(value for value in (
            previous.pass1_ms, current.pass1_ms,
        ) if value is not None) or None,
        pass2_ms=sum(value for value in (
            previous.pass2_ms, current.pass2_ms,
        ) if value is not None) or None,
        pass1_text="；".join(filter(None, (
            previous.pass1_text, current.pass1_text,
        ))) or None,
        corrections=previous.corrections + current.corrections,
        suspicious=previous.suspicious + current.suspicious,
        is_fragment=previous.is_fragment and current.is_fragment,
        preprocess_text="；".join(filter(None, (
            previous.preprocess_text or previous.text,
            current.preprocess_text or current.text,
        ))) or None,
    )


class AdjacentFinalMerger:
    """问题检测前合并同声道、同说话人的未完结 final。"""

    def __init__(self, window_ms: int = 1500):
        self.window_ms = window_ms
        self._pending: Transcript | None = None

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def push(self, transcript: Transcript) -> list[Transcript]:
        if transcript.kind is not TranscriptKind.FINAL:
            return []
        ready: list[Transcript] = []
        if self._pending is not None:
            previous = self._pending
            gap_ms = transcript.start_ms - previous.end_ms
            speaker_matches = (
                not previous.speaker_id or not transcript.speaker_id
                or previous.speaker_id == transcript.speaker_id
            )
            can_merge = (
                previous.channel is transcript.channel
                and speaker_matches
                and 0 <= gap_ms < self.window_ms
                and should_merge_after(previous.text)
            )
            self._pending = None
            if can_merge:
                transcript = _combine_final_transcripts(previous, transcript)
            else:
                ready.append(previous)

        if should_merge_after(transcript.text):
            self._pending = transcript
        else:
            ready.append(transcript)
        return ready

    def flush(self) -> Transcript | None:
        pending, self._pending = self._pending, None
        return pending


class ChannelSpeakerGate:
    """只允许指定提问声道和可选 speaker 进入问答。"""

    def __init__(
        self,
        question_speaker_id: str | None = None,
        question_channel: Channel = Channel.LOOPBACK,
    ):
        self.question_speaker_id = question_speaker_id
        self.question_channel = question_channel

    def allows(self, transcript: Transcript) -> bool:
        if transcript.channel is not self.question_channel:
            return False
        if self.question_speaker_id and transcript.speaker_id != self.question_speaker_id:
            return False
        return True


class RuleQuestionDetector:
    """规则先筛，轻量分类器可选增强；最终阈值固定为 0.6。"""

    def __init__(
        self,
        gate: ChannelSpeakerGate | None = None,
        classifier: Callable[[str, Sequence[Transcript]], float | Awaitable[float]] | None = None,
        threshold: float = 0.6,
    ):
        self.gate = gate or ChannelSpeakerGate()
        self.classifier = classifier
        self.threshold = threshold

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", (text or "").strip())

    @staticmethod
    def _rewrite(query: str, context: Sequence[Transcript]) -> str:
        if not (_PRONOUN.search(query) or len(query) <= 6):
            return query
        for previous in reversed(context):
            if previous.kind is TranscriptKind.FINAL and previous.text.strip() and previous.text.strip() != query:
                return previous.text.strip("。！？!? ") + "；" + query
        return query

    async def evaluate(self, transcript: Transcript, context: Sequence[Transcript]) -> QuestionDecision:
        text = self._normalize(transcript.text)
        if transcript.kind is not TranscriptKind.FINAL:
            return QuestionDecision(False, 0.0, text, "partial")
        if not self.gate.allows(transcript):
            return QuestionDecision(False, 0.0, text, "channel_or_speaker_filtered")
        if len(text) < 3 or _SMALL_TALK.fullmatch(text):
            return QuestionDecision(False, 0.05, text, "small_talk")

        score = 0.2
        reasons = []
        if _QUESTION_WORDS.search(text):
            score += 0.45
            reasons.append("question_word")
        if text.endswith(("?", "？")):
            score += 0.2
            reasons.append("question_mark")
        if _COMMAND.search(text):
            score += 0.45
            reasons.append("instruction")
        if is_incomplete_ending(text):
            score -= 0.35
            reasons.append("incomplete")

        if self.classifier:
            model_score = self.classifier(text, context)
            if inspect.isawaitable(model_score):
                model_score = await model_score
            model_score = max(0.0, min(1.0, float(model_score)))
            # 规则负责可解释召回，模型负责语义校准；二者均有影响但不让模型独断。
            score = score * 0.6 + model_score * 0.4
            reasons.append("classifier")

        score = max(0.0, min(1.0, score))
        normalized = self._rewrite(text, context)
        return QuestionDecision(score >= self.threshold, score, normalized, "+".join(reasons) or "statement")


@dataclass(slots=True)
class _PendingQuestion:
    transcript: Transcript
    decision: QuestionDecision


class SlidingQuestionMerger:
    """同声道、同 speaker 的未完结问题在短窗口内合并。"""

    def __init__(self, window_ms: int = 1500):
        self.window_ms = window_ms
        self._pending: _PendingQuestion | None = None

    def push(self, transcript: Transcript, decision: QuestionDecision) -> tuple[Transcript, QuestionDecision] | None:
        if not decision.is_question:
            return None
        if self._pending is None:
            self._pending = _PendingQuestion(transcript, decision)
            return None

        previous = self._pending
        same_speaker = previous.transcript.speaker_id == transcript.speaker_id
        same_channel = previous.transcript.channel is transcript.channel
        gap_ms = transcript.start_ms - previous.transcript.end_ms
        if (
            same_speaker
            and same_channel
            and 0 <= gap_ms < self.window_ms
            and should_merge_after(previous.transcript.text)
        ):
            merged_query = previous.decision.normalized_query.rstrip("。！？!? ") + "；" + decision.normalized_query
            merged_transcript = _combine_final_transcripts(previous.transcript, transcript)
            merged_decision = QuestionDecision(
                True,
                max(previous.decision.confidence, decision.confidence),
                merged_query,
                "merged",
            )
            self._pending = _PendingQuestion(merged_transcript, merged_decision)
            return None

        ready = (previous.transcript, previous.decision)
        self._pending = _PendingQuestion(transcript, decision)
        return ready

    def pop_ready(self, now_ms: int, force: bool = False) -> tuple[Transcript, QuestionDecision] | None:
        if self._pending is None:
            return None
        if not force and now_ms - self._pending.transcript.end_ms < self.window_ms:
            return None
        ready = (self._pending.transcript, self._pending.decision)
        self._pending = None
        return ready
