# ASR 阶段 2：partial/final 状态机报告

## 结论

已修复实时工作台下一句 partial 携带历史 final 的问题。旧会议助手仍保留累计全文接口，
本阶段没有修改模型、VAD、热词、RAG 或打包配置。

## 状态转换

单元测试覆盖：

```text
partial(utterance A, 第一)
→ partial(utterance A, 第一句)
→ final(utterance A, 第一句)
→ partial(utterance B, 新)
→ final(utterance B, 新问题)
```

真实 sherpa-onnx 连续双句验证结果：

```text
utterance A: partial 鲜 → … → final 鲜艳矿机制是什么呀
utterance B: partial US → USB → … → final USB的无线传输协议有哪些
```

第二句 partial 未包含第一句 final，两个 utterance ID 不同。

## 指标回归

阶段 2 没有改变识别模型，使用同一组 33 条音频快速回放验证质量指标不下降：

| 指标 | 阶段 1 基线 | 阶段 2 | 变化 |
|---|---:|---:|---:|
| CER | 16.60% | 16.60% | 0 |
| 热词召回率 | 51.72% | 51.72% | 0 |
| 术语误识率 | 48.28% | 48.28% | 0 |
| 问题级准确率 | 39.39% | 39.39% | 0 |

快速回放不用于比较延迟；正式延迟基线仍以 `baseline.md` 的实时节奏结果为准。

## 自动化验证

- 状态机、WebSocket ID 与前端接线专项测试：5 passed
- 完整 `tests/`：79 passed
- 真实模型连续双句状态检查：PASS

## 仍存在的问题

“先验框”仍会被识别为“鲜艳矿”，这是模型/术语准确率问题，属于后续两遍识别和词库阶段，
不在本阶段通过硬编码修改。
