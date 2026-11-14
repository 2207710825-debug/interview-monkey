# 阶段 8：VAD 与断句调优

## 诊断结论

阶段 6 已有 300/500/700 ms 三档静音阈值、200 ms pre-roll 和 150 ms post-roll。主要缺口是未完句识别未覆盖“是、那个”等词尾；相邻 final 的旧合并发生在问题检测之后且窗口为 3 秒，无法按本阶段要求在检测前合并。

## 改动

- 在 `engine/realtime/utterance.py` 统一识别未完句词尾和句末语气。VAD 对未完句延长到 incomplete 阈值；明确问号/感叹号优先按完整句处理，避免“产生的？”被误判为未完句。
- `engine/realtime/vad/adaptive.py` 使用统一的未完句判定。常规/快速/未完结阈值分别保持 500/300/700 ms，pre-roll/post-roll 为 200/150 ms。
- `engine/realtime/qa/detector.py` 增加检测前 final 合并器；`engine/realtime/orchestrator.py` 将它接入实时链路。间隔必须小于 1.5 秒，声道一致；说话人可识别时也要求一致。前句有句末语气时直接进入检测，不额外等待。
- `engine/realtime/config.py` 校验阈值均在 300–700 ms、满足 fast ≤ base ≤ incomplete，且 pre-roll/post-roll 分别不少于 200/150 ms。
- `eval/asr/evaluate.py` 接受临时 VAD 参数覆盖；新增 `eval/asr/tune_vad.py`，不改写项目配置，真实运行 33 条本地两遍 ASR 评测并生成 Markdown、JSON、SVG CER 曲线。

## 网格搜索与基线

固定 fast=300 ms、incomplete=700 ms，只搜索常规 `base_silence_ms`：

| base (ms) | CER | 术语召回率 | 问题级准确率 |
|---:|---:|---:|---:|
| 300 | 3.1594% | 98.28% | 93.94% |
| 400 | 3.7963% | 96.55% | 93.94% |
| 500 | **3.1145%** | **100.00%** | **100.00%** |
| 600 | **3.1145%** | **100.00%** | **100.00%** |
| 700 | 3.4933% | 98.28% | 96.97% |

最优点为 500/600 ms 并列；选择 500 ms，因为它具有相同 CER、术语召回和问题准确率，且常规静音等待更短。阶段 6 快速基线为 CER 3.1145%、术语召回 100%、问题准确率 100%；阶段 8 的最优参数与基线一致，没有观察到回退。当前 `config/config.yaml` 已经使用 base=500 ms，因此无需更改运行默认值。

完整曲线、所有阈值的延迟诊断数值和机器可读数据见 [phase8_vad_grid.md](phase8_vad_grid.md)、[phase8_vad_grid.svg](phase8_vad_grid.svg)、[phase8_vad_grid.json](phase8_vad_grid.json)。快速离线回放的延迟仅作诊断，不作为实时延迟验收结果。

## 验证与限制

- `python -X utf8 -m pytest tests -q`：116 项通过，7 个边界子用例通过。
- `python -X utf8 -m compileall -q engine eval`：通过。
- 网格搜索：33 条样本 × 5 档配置全部完成；JSON 有效、SVG XML 可解析。
- 当前评测集 33 条均为 TTS/加噪 TTS，缺少人工校对的真人及真实会议环回样本。阈值结果需要后续用真人样本复核。

复现命令：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\tune_vad.py --values 300 400 500 600 700
```

## 参考

- GitHub：[OpenAI Realtime VAD 字段说明](https://github.com/openai/openai-python/blob/main/src/openai/types/realtime/realtime_session_create_response.py)描述静音时长参数与响应速度/提前打断之间的取舍；[NVIDIA Nemotron Speech endpointing 文档](https://github.com/NVIDIA/skills/blob/main/skills/nemotron-speech/references/pipelines.md)列出静音窗口和 onset/offset padding 参数。
- 知乎：[VAD 语音端点判断讨论](https://www.zhihu.com/en/answer/350281046)和 [Sherpa-ONNX 本地语音助手实践](https://zhuanlan.zhihu.com/p/2036543299289346711)作为中文场景补充参考；阈值取舍以本项目的 33 条评测结果为准。
