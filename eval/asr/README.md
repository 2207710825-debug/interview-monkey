# ASR 阶段 1 基线

本目录只负责建立可重复的识别基线，不修改实时识别链路。

运行：

```powershell
cd E:\面试猴\面试猴-app
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py
```

脚本会校验 `cases.yaml` 至少包含 30 条样本，缺少 WAV 时通过 Windows SAPI
生成 16 kHz、单声道、PCM16 音频，然后按实时节奏回放给项目当前的
`AdaptiveVadSegmenter → StreamingSession → HotwordCorrector` 链路。

输出报告：`eval/asr/reports/baseline.md`。

快速检查可使用 `--fast`，但该模式的延迟数据不作为验收依据。使用
`--refresh-tts` 可强制重建全部合成样本。

阶段 3 的轻量 AGC A/B 可使用：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py --fast --agc `
  --report eval\asr\reports\phase3_agc_fast.md
```

阶段 4 的本地两遍识别评测使用：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py --two-pass `
  --report eval\asr\reports\phase4_two_pass_realtime.md
```

该命令以 sherpa Transducer 提供流式 partial，在显式 `utterance_end` 后用
SenseVoice 对整句音频执行第二遍解码。只有第二遍 final（失败或超时时才回退到
sherpa final）会进入问题检测与 RAG。快速回归可追加 `--fast`，但延迟不作为验收依据。

阶段 5 使用 `config/glossary.yaml` 作为正式领域词库。生成知识库候选词：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 scripts\extract_glossary_candidates.py
```

输出为 `config/glossary.candidates.yaml`，所有条目均为 `pending`，不会自动并入正式词库。
完成词库回归：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py --two-pass `
  --report eval\asr\reports\phase5_glossary_realtime.md
```

阶段 6 在两遍识别后运行本地确定性文本后处理（口癖清理、ITN、显式词库别名、阈值拼音纠错），并把后处理前后的文本与 CER 同时写入报告。可选本地 LLM 纠错只在实时问答中运行，不参与离线 CER 评测：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py --two-pass `
  --report eval\asr\reports\phase6_postprocess_realtime.md
```

拼音纠错依赖 `pypinyin`，已加入 `requirements-realtime.txt`；新环境请重新安装该依赖文件。

## 阶段 9：问答日志、回放与 CI 门禁

实时语音和文本问答都会追加到本机 JSONL：
`%LOCALAPPDATA%\InterviewMonkey\logs\qa.jsonl`。可用 `MH_QA_LOG_PATH` 覆盖日志文件路径。
日志包含 raw/corrected 文本、规则纠错、ASR 引擎与两遍耗时、检索 top-k 分数、回答、状态和端到端延迟。

为了避免默认长期保存面试语音，`config/config.yaml` 中的
`observability.record_audio_for_replay` 默认是 `false`。用户明确需要音频回放时，将其设为
`true`；命中的 VAD 音频片段会只写入本机日志目录下的 `audio/`，日志记录相应路径，不上传到云端。

重放 JSONL 问答记录（默认使用 Mock LLM，仍运行本地 ASR、问题检测和 RAG）：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 scripts\replay.py `
  --log "$env:LOCALAPPDATA\InterviewMonkey\logs\qa.jsonl" --limit 5
```

也可将阶段评测 JSON 报告作为音频与 gold 文本清单：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 scripts\replay.py `
  --log eval\asr\reports\phase6_postprocess_fast.json --limit 3
```

生产 ASR 质量门禁以当前最佳已验证的阶段 6 两遍结果为基准，而不是最初较弱的阶段 1 基线：

```powershell
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py --two-pass --fast `
  --report eval\asr\reports\ci_candidate.md
.\.venv-pack\Scripts\python.exe -X utf8 scripts\check_asr_regression.py `
  --candidate eval\asr\reports\ci_candidate.json
```

GitHub Actions 的单元测试在 Windows runner 执行；需要实际 33 条音频推理的质量门禁使用带
`asr-models` 标签且已准备本地模型的 Windows self-hosted runner，避免每次 CI 下载数百 MB 模型。
词库测试要求 `tests/fixtures/glossary_cases.yaml` 为每个正式术语提供至少一条明确输入/期望用例；
新增词条时必须同时新增对应测试项。

## 真人/真实环回样本

TTS 只能提供可重复的工程回归，不能代替真实场景。加入真人或真实会议环回样本时：

1. 将人工确认过标准文本的 16 kHz/mono/PCM16 WAV 放入 `samples/`。
2. 在 `cases.yaml` 增加对应条目，`source` 写 `human` 或 `loopback_real`。
3. `reference` 必须由人工听写确认，禁止把任一 ASR 输出当作标准答案。
4. 重新运行同一条命令，报告会自动统计真实样本数量。
