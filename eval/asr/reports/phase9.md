# 阶段 9：可观测性与回归

## 交付内容

- 语音问答和文本问答都追加结构化 JSONL 记录。字段包括 `raw_text`、`corrected_text`、`corrections`、`asr_engine`、`pass1_ms`、`pass2_ms`、带分数的 `retrieval_top_k`、检索 query、最终问题、回答、状态及端到端延迟。
- 默认日志位置为 `%LOCALAPPDATA%\InterviewMonkey\logs\qa.jsonl`，可通过 `MH_QA_LOG_PATH` 改写。写日志失败不会阻断当前回答。
- `observability.record_audio_for_replay` 默认关闭。打开后，程序仅暂存 VAD 语音段到当前问题判定完成；只有成为问答的问题音频才写入日志目录的 `audio/`，本机 WAV 路径记入 JSONL，不上传音频。
- 新增 `scripts/replay.py`：读取问答 JSONL 或 ASR 评测 JSON，播放本地 WAV，运行当前本地 VAD、ASR、问题检测、RAG 与回答链路；默认使用 Mock LLM 保证回放稳定，可选择使用配置中的本地 LLM。
- 新增 `scripts/check_asr_regression.py` 与 `.github/workflows/asr-regression.yml`。门禁以阶段 6/8 已验证的最佳两遍结果为严格基准：候选 CER 不得上升，术语召回率不得下降。词库回归夹具为 170 个现有术语提供明确用例；词库新增条目若未同时补充 `tests/fixtures/glossary_cases.yaml` 会使测试失败。

## 现状基线与本阶段验证

| 指标 | 阶段 1 基线 | 阶段 9 完成后的本地 33 条评测 | 变化 |
|---|---:|---:|---:|
| CER（macro） | 16.6015% | 3.1145% | 降低 13.4870 个百分点（相对降低约 81.24%） |
| 术语召回率 | 51.72% | 100.00% | 提升 48.28 个百分点 |
| 问题级准确率 | 39.39% | 100.00% | 提升 60.61 个百分点 |

本阶段重新运行了 33 条本地两遍 ASR 快速评测，得到 CER 3.1145%、术语召回率 100%、问题级准确率 100%，与阶段 6/8 当前黄金报告一致。新的基线门禁针对这次生成的候选 JSON 实际运行并通过。延迟数据是快速离线回放数据，不作为实时延迟验收。

另外，`scripts/replay.py` 对 `asr001` 成功完成一次完整链路回放：CER 11.111%，问题检测通过。该值是单条样本的 CER，不能代表整套评测成绩。

验证结果：

- `python -X utf8 -m pytest tests -q`：125 项通过，177 个子用例通过。
- `python -X utf8 -m compileall -q engine eval scripts`：通过。
- ASR 回归门禁：候选与阶段 6 基准 CER 均为 3.114478%，术语召回率均为 100%，通过。
- 本机执行了工作流中的评测与门禁命令；GitHub Actions 本身未在本次运行中触发。线上质量任务需要配置带 `asr-models` 标签、预装模型资产的 Windows self-hosted runner。

## 使用方法

实时问答元数据默认写入本机 JSONL。需要将问答音频存档用于重放时，在 `config/config.yaml` 设置：

```yaml
observability:
  record_audio_for_replay: true
```

重放最新本地问答记录：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 scripts\replay.py `
  --log "$env:LOCALAPPDATA\InterviewMonkey\logs\qa.jsonl" --limit 5
```

本地重新评测并检查门禁：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py --two-pass --fast `
  --report eval\asr\reports\ci_candidate.md
.\.venv-pack\Scripts\python.exe -X utf8 scripts\check_asr_regression.py `
  --candidate eval\asr\reports\ci_candidate.json
```

## 参考项目

- GitHub 的 [asr-eval](https://github.com/lmbgithub/asr-eval) 使用逐条 JSONL 清单保留音频路径等元数据，并对照提交的基准结果设置可自动失败的回归门禁；本项目沿用 JSONL/机器可读报告思路，门禁计算使用项目现有 CER 和术语召回口径。
- GitHub 的 [中文 asr-eval](https://github.com/lixuanqun/asr-eval) 强调使用人工校对参考文本并复现 CER；本项目继续使用 `cases.yaml` 的标注参考文本和本地 33 条 WAV。
- 知乎的 [AmphionASR 热词与多任务评测介绍](https://zhuanlan.zhihu.com/p/2075183470100341457) 作为中文热词与 CER 评测背景阅读；工程门禁数值仍以本项目固定评测集实测为准。

评测样本当前为 30 条 TTS 和 3 条加噪 TTS，尚缺人工校对的真人及真实会议环回样本。音频归档默认关闭，启用后会保存命中问题检测的语音片段；这些文件和结构化文本包含面试内容，应按本机数据管理要求处理。
