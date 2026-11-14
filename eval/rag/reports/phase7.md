# 阶段 7：ASR 容错与检索回退

## 完成内容

- 回答提示词增加 ASR 近音术语消歧规则：结合上下文和词库候选判断；候选可信时先简短说明采用的术语，只有无法判断时才请用户复述。
- 从问题转写整理分数最高的 3 个疑似词库候选，将原词、候选术语和匹配分数传给纠错器与回答提示词。
- 对纠正问题、原始转写及热词中匹配术语的别名分别检索，并使用 RRF 按跨查询排名融合、按 chunk 去重。
- 对低分命中的产品/FAQ 文档，在通用技术问题中跳过此类片段，避免把产品说明当成技术依据。无资料时的提示词要求按通用知识回答并标注“（通用知识）”；产品私有事实仍须说明无法从现有资料确认。
- 知识片段保存首层目录分类；知识库 API 和页面下拉框支持“全部知识库”及按分类切换。
- 修正分类器对“背压、TTFT、WDA_EXCLUDEFROMCAPTURE、说话人分离”等术语的漏判。

## 检索评测

运行 `python -X utf8 eval/rag/run_retrieval_eval.py --json-out eval/rag/reports/phase7_retrieval.json`，使用现有 20 道题、共 60 个必需术语，对比单 query 基线和阶段 7 检索。结果保存在同目录的 `phase7_retrieval.json`。

| 指标 | 单 query 基线 | 阶段 7 |
| --- | ---: | ---: |
| 有召回结果的问题 | 20/20 | 0/20 |
| 必需术语命中 | 3/60（5%） | 0/60（0%） |
| 低置信产品文档过滤 | — | 20/20 |
| 返回的产品文档片段 | 40 | 0 |

这组结果不是知识召回变好了：当前 `engine/knowledge` 只有两份产品/使用说明文档。基线虽然每题都能拿到片段，但多数是与技术题无关的产品资料；阶段 7 将这些低置信片段过滤掉，因此进入“无证据”回退。要提高技术题召回，需要把经过审核的领域资料放入知识库，例如 `engine/knowledge/目标检测/`、`engine/knowledge/数据库/`，页面会自动提供相应分类选项。

该脚本只测检索片段是否包含题目标注术语，不调用 LLM，也不能证明通用知识回答的事实正确性。真实 Ollama/云端模型下的自然度、术语纠错正确率和回答质量尚未做实机验收。

## 验证

- 阶段相关测试：28 项通过；随后全量套件 `python -X utf8 -m pytest tests -q`：113 项通过。
- 语法编译检查：`python -X utf8 -m compileall -q engine eval` 通过。
- 检索评测可复现命令：`python -X utf8 eval/rag/run_retrieval_eval.py --json-out eval/rag/reports/phase7_retrieval.json`。

## 参考实现

- [Microsoft Architecture Center：RAG information retrieval](https://github.com/MicrosoftDocs/architecture-center/blob/main/docs/ai-ml/guide/rag/rag-information-retrieval.md)：说明查询扩展/改写以及多路排名结果使用 RRF 融合的做法。
- [Microsoft RAG Experiment Accelerator](https://github.com/microsoft/rag-experiment-accelerator)：用于对照检索实验和查询扩展流程。
- 知乎站内检索到的 [文章 1](https://zhuanlan.zhihu.com/p/2042202041557177202) 和 [文章 2](https://zhuanlan.zhihu.com/p/2063364521142630241) 作为中文实践线索；本轮网页读取被知乎返回 403，未据其内容作技术结论。
