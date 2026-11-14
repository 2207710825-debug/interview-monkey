# -*- coding: utf-8 -*-
"""pipeline 单元测试：用假时钟驱动 QuestionAggregator，覆盖验收用例 (a)-(d)。"""
import unittest
import sys
import os

# 把项目根目录加进 sys.path，方便 import engine
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import pipeline as pipe


class FakeClock:
    """假时钟：手动推进时间，避免真实等待。"""
    def __init__(self, start=1000.0):
        self.t = start
    def __call__(self):
        return self.t
    def advance(self, dt):
        self.t += dt


class TestPipeline(unittest.TestCase):

    def _make_agg(self, clock, **kw):
        """建一个聚合器，收集触发的问题。返回 (agg, fired_list)。"""
        fired = []
        agg = pipe.QuestionAggregator(
            on_question=lambda merged: fired.append(merged),
            now=clock,
            **kw
        )
        return agg, fired

    # ---- (a) "这是一个什么" 之后 1 秒内来 "是交并比" → 合并成一个问题，只触发 1 次 ----
    def test_merge_two_fragments_into_one_question(self):
        clk = FakeClock()
        agg, fired = self._make_agg(clk)
        agg.push_final("这是一个什么")          # 第一句，命中"什么"
        clk.advance(1.0)                         # 1 秒后——还没到 quiet_q(1.2)，先不触发
        agg.tick()
        self.assertEqual(fired, [])              # 此时不该触发
        agg.push_final("是交并比")               # 第二句紧接而来，刷新 last
        clk.advance(1.3)                         # 超过 quiet_q(1.2)，该触发了
        agg.tick()
        # 合并后应是一条完整问题，且只触发 1 次
        self.assertEqual(len(fired), 1)
        self.assertIn("什么", fired[0])
        self.assertIn("交并比", fired[0])

    # ---- (b) "好的""嗯" → 触发 0 次 ----
    def test_small_talk_no_fire(self):
        clk = FakeClock()
        agg, fired = self._make_agg(clk)
        agg.push_final("好的")
        clk.advance(3.0)                         # 远超 quiet_any(2.5)
        agg.tick()
        self.assertEqual(fired, [])              # 非问句，不调 LLM

    # ---- (c) 停顿 ≥2.5 秒的非问句 → 触发 0 次 ----
    def test_non_question_long_pause_no_fire(self):
        clk = FakeClock()
        agg, fired = self._make_agg(clk)
        agg.push_final("今天天气不错")           # 非问句
        clk.advance(2.6)                         # 超过 quiet_any(2.5)
        agg.tick()
        self.assertEqual(fired, [])              # 非问句只留在转写里

    # ---- (d) "SKIP""[skip]。" 被过滤 ----
    def test_skip_text_filtered(self):
        # is_skip_text 应匹配纯 skip 标记
        self.assertTrue(pipe.is_skip_text("SKIP"))
        self.assertTrue(pipe.is_skip_text("[skip]"))
        self.assertTrue(pipe.is_skip_text("[skip]。"))
        self.assertTrue(pipe.is_skip_text("  skip  "))
        # 正常回答不该被过滤
        self.assertFalse(pipe.is_skip_text("CIoU 是一种边界框回归损失函数"))
        self.assertFalse(pipe.is_skip_text("[SKIP] 开始回答内容"))


if __name__ == "__main__":
    unittest.main()
