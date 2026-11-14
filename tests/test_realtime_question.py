# -*- coding: utf-8 -*-
import asyncio
import unittest

from engine.realtime.contracts import Channel, Transcript, TranscriptKind
from engine.realtime.qa.detector import (
    AdjacentFinalMerger,
    ChannelSpeakerGate,
    RuleQuestionDetector,
    SlidingQuestionMerger,
)
from engine.realtime.qa.work import QuestionWorkController


def final(text, *, channel=Channel.LOOPBACK, speaker="speaker-1", start=0, end=1000):
    return Transcript(TranscriptKind.FINAL, text, channel, start, end, speaker, 0.9)


class TestRealtimeQuestion(unittest.IsolatedAsyncioTestCase):
    async def test_mic_never_triggers(self):
        detector = RuleQuestionDetector()
        result = await detector.evaluate(final("请解释RAG是什么", channel=Channel.MIC), [])
        self.assertFalse(result.is_question)
        self.assertEqual(result.reason, "channel_or_speaker_filtered")

    async def test_selected_speaker_only(self):
        detector = RuleQuestionDetector(ChannelSpeakerGate("interviewer"))
        rejected = await detector.evaluate(final("请解释Kafka", speaker="other"), [])
        accepted = await detector.evaluate(final("请解释Kafka", speaker="interviewer"), [])
        self.assertFalse(rejected.is_question)
        self.assertTrue(accepted.is_question)

    async def test_small_talk_and_incomplete_are_filtered(self):
        detector = RuleQuestionDetector()
        self.assertFalse((await detector.evaluate(final("好的"), [])).is_question)
        self.assertFalse((await detector.evaluate(final("为什么这样，因为"), [])).is_question)

    async def test_pronoun_query_rewrite(self):
        detector = RuleQuestionDetector()
        context = [final("Kafka如何保证消息可靠", start=0, end=1000)]
        result = await detector.evaluate(final("它的原理呢", start=2000, end=2500), context)
        self.assertTrue(result.is_question)
        self.assertIn("Kafka", result.normalized_query)

    async def test_merge_same_speaker_within_short_window(self):
        detector = RuleQuestionDetector()
        first = final("什么是RAG", start=0, end=1000)
        second = final("它有什么优点", start=2300, end=2800)
        merger = SlidingQuestionMerger()
        self.assertIsNone(merger.push(first, await detector.evaluate(first, [])))
        self.assertIsNone(merger.push(second, await detector.evaluate(second, [first])))
        self.assertIsNone(merger.pop_ready(4200))
        ready = merger.pop_ready(4301)
        self.assertIsNotNone(ready)
        self.assertIn("RAG", ready[1].normalized_query)
        self.assertIn("优点", ready[1].normalized_query)

    async def test_adjacent_incomplete_finals_merge_before_question_detection(self):
        detector = RuleQuestionDetector()
        merger = AdjacentFinalMerger(window_ms=1500)
        first = final("什么是RAG的", start=0, end=500)
        second = final("检索增强生成机制？", start=1200, end=1800)
        self.assertEqual(merger.push(first), [])
        merged_batch = merger.push(second)
        self.assertEqual(len(merged_batch), 1)
        self.assertIn("什么是RAG的，检索增强生成机制？", merged_batch[0].text)
        decision = await detector.evaluate(merged_batch[0], [])
        self.assertTrue(decision.is_question)

    def test_adjacent_merge_requires_same_channel_speaker_and_gap_below_1500ms(self):
        first = final("先解释这个的", start=0, end=500)
        complete = final("下一句是完整问题吗？", start=1000, end=1400)

        too_late = AdjacentFinalMerger()
        too_late.push(first)
        exact_boundary = final("下一句是完整问题吗？", start=2000, end=2400)
        self.assertEqual(len(too_late.push(exact_boundary)), 2)

        different_speaker = AdjacentFinalMerger()
        different_speaker.push(first)
        other_speaker = final("下一句是完整问题吗？", start=1000, end=1400, speaker="speaker-2")
        separate = different_speaker.push(other_speaker)
        self.assertEqual([item.text for item in separate], [first.text, other_speaker.text])

        different_channel = AdjacentFinalMerger()
        different_channel.push(first)
        other_channel = final("下一句是完整问题吗？", channel=Channel.MIC, start=1000, end=1400)
        separate = different_channel.push(other_channel)
        self.assertEqual([item.text for item in separate], [first.text, other_channel.text])

        complete_merger = AdjacentFinalMerger()
        self.assertEqual(complete_merger.push(complete), [complete])

    async def test_new_work_cancels_previous(self):
        interrupted = []
        controller = QuestionWorkController(interrupted.append)
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(10)

        old = await controller.start("old", slow)
        await started.wait()
        new = await controller.start("new", lambda: asyncio.sleep(0))
        await new
        self.assertTrue(old.cancelled())
        self.assertEqual(interrupted, ["old"])


if __name__ == "__main__":
    unittest.main()
