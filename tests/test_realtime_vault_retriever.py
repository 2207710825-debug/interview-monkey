# -*- coding: utf-8 -*-
import unittest

from engine.realtime.contracts import Chunk
from engine.realtime.rag.vault import VaultRetriever, _is_meta_section


def chunk(cid, text, source, section, category="3D"):
    return Chunk(
        id=cid, text=text, source=source, score=0.0, section=section,
        metadata={"category": category},
    )


class TestVaultRetriever(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chunks = [
            chunk(
                "icp",
                "ICP 对初值敏感，容易陷入局部最优。改进：点到面 ICP、GICP、NDT、"
                "FPFH + RANSAC 粗配准，再用 KD-Tree 和体素降采样降低开销。",
                "3D学习笔记/位姿估计.md",
                "第三步：精细化微调（ICP 算法出场）",
            ),
            chunk(
                "yolo",
                "YOLO 目标检测训练流程：正负样本分配、类别损失、框回归。",
                "其他学习笔记/YOLO目标检测.md",
                "YOLO 目标检测快速理解",
                category="视觉",
            ),
            chunk(
                "nms",
                "Soft-NMS 不直接删除重叠框，而是按 IoU 降分；DIoU-NMS 同时考虑中心点距离，"
                "缓解密集目标误删。",
                "其他学习笔记/检测后处理.md",
                "NMS 后处理",
                category="视觉",
            ),
            chunk(
                "meta",
                "ICP 是什么？怎么改进？请闭卷回答。",
                "按老师/自测.md",
                "闭卷自测",
            ),
        ]
        self.retriever = VaultRetriever(self.chunks, confidence_threshold=0.5)

    async def test_icp_query_returns_icp_not_meta(self):
        result = await self.retriever.retrieve("ICP 的缺点是什么？怎么改进？", top_k=4)
        self.assertTrue(result)
        self.assertEqual(result[0].id, "icp")
        self.assertNotIn("meta", [item.id for item in result])

    async def test_meta_section_is_not_indexed(self):
        self.assertEqual([c.id for c in self.retriever.chunks], ["icp", "yolo", "nms"])

    def test_wechat_quick_reference_is_kept_but_self_test_is_filtered(self):
        self.assertFalse(_is_meta_section(
            "21. 经典网络结构标注速查（2026-11-13 追加） > 21.5 U-Net 结构标注",
            "微信资料/深度学习与UNet学习笔记.md",
        ))
        self.assertFalse(_is_meta_section(
            "附录 A：术语速查",
            "微信资料/深度学习与UNet学习笔记.md",
        ))
        self.assertTrue(_is_meta_section(
            "4.7.8 自测（合上书能答这几条就算过）",
            "微信资料/深度学习与UNet学习笔记.md",
        ))
        self.assertTrue(_is_meta_section(
            "目录（按主题查，不必按顺序背）",
            "微信资料/面试高频题_答题骨架_深度学习基础_修订版.md",
        ))

    async def test_nms_expansion_finds_soft_nms(self):
        result = await self.retriever.retrieve("NMS 在密集目标下误删怎么改进？", top_k=4)
        self.assertTrue(result)
        self.assertEqual(result[0].id, "nms")

    async def test_unrelated_query_falls_back(self):
        result = await self.retriever.retrieve("量子计算是什么？", top_k=4)
        self.assertEqual(result, [])

    async def test_anchor_term_gate_rejects_weak_chunk(self):
        # 查询 NMS，但库中只有一个无关 YOLO chunk 时，不应强行返回。
        retriever = VaultRetriever([chunk("yolo", "YOLO 训练流程。", "yolo.md", "YOLO")])
        result = await retriever.retrieve("NMS 密集目标误删怎么改进？", top_k=4)
        self.assertEqual(result, [])

    async def test_chinese_anchor_alias_matches_stereo_rectification(self):
        retriever = VaultRetriever([
            chunk(
                "stereo-rectification",
                "立体校正后，同名点应处在同一条水平线，搜索从二维变为一维。",
                "stereo.md",
                "为什么要立体校正",
            )
        ])
        result = await retriever.retrieve("极线校正的作用是什么？", top_k=4)
        self.assertEqual([item.id for item in result], ["stereo-rectification"])

    async def test_chinese_anchor_alias_rejects_unrelated_chunk(self):
        retriever = VaultRetriever([
            chunk(
                "yolo",
                "YOLO 训练包括数据增强、正负样本分配和损失计算。",
                "yolo.md",
                "YOLO 训练",
                category="视觉",
            )
        ])
        result = await retriever.retrieve("极线校正的作用是什么？", top_k=4)
        self.assertEqual(result, [])

    async def test_multiword_phrase_matches_single_body_mention(self):
        retriever = VaultRetriever([
            chunk(
                "focal",
                "focal loss 用于缓解类别不平衡。",
                "loss.md",
                "检测损失",
            )
        ])
        result = await retriever.retrieve("Focal Loss 解决什么问题？", top_k=4)
        self.assertEqual([item.id for item in result], ["focal"])

    async def test_phrase_gate_does_not_require_other_single_latin_anchor(self):
        retriever = VaultRetriever([
            chunk(
                "faster-r-cnn",
                "Faster R-CNN 通过 RPN 生成候选区域。",
                "two-stage.md",
                "两阶段检测器",
            )
        ])
        result = await retriever.retrieve("Faster R-CNN 的 RPN 有什么作用？", top_k=4)
        self.assertEqual([item.id for item in result], ["faster-r-cnn"])

    async def test_phrase_gate_rejects_partial_roi_mentions(self):
        retriever = VaultRetriever([
            chunk(
                "roi",
                "RoI 表示候选区域，这里只介绍区域概念。",
                "roi.md",
                "RoI 基础",
            )
        ])
        result = await retriever.retrieve("RoI Align 和 RoI Pooling 有什么区别？", top_k=4)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
