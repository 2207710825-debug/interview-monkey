# 阶段 4：本地两遍识别与模型选型

## 结论

在当前已经具备本地模型、无需联网下载的方案中，采用：

- 第一遍：sherpa-onnx Transducer，持续输出低延迟 partial。
- 第二遍：SenseVoice，在显式整句结束后重新解码完整语音。
- 下游约束：第二遍 final 是进入问题检测与 RAG 的唯一文本；第二遍失败或超时时才回退到第一遍 final。

正式实时节奏回放中，CER 从 16.60% 降到 7.69%，相对下降 53.7%，达到“相对下降至少 40%”的目标。热词召回率从 51.72% 升到 72.41%，仍未达到 90%，需要阶段 5 的领域词表与上下文纠偏继续处理。

## 引擎可用性与选择

| 方案 | 本机状态 | 本阶段结论 |
|---|---|---|
| sherpa-onnx Transducer | 已安装，模型已存在 | 保留为流式第一遍和可靠回退 |
| SenseVoice | 模型已存在 | 选为默认第二遍，33/33 句成功 |
| FunASR Paraformer / SeACo | 包和模型均未安装 | 未下载、未虚构评测结果 |
| faster-whisper medium / large-v3-turbo | 包和模型均未安装 | 未下载、未虚构评测结果 |

本阶段没有联网下载多 GB 模型，也没有接入云端识别服务。

## 正式前后对比

评测集为同一组 33 条可重复 TTS/噪声样本。当前仍没有人工校对的真人或真实会议环回样本，因此这份结果是工程回归证据，不是最终真实场景验收。

| 指标 | 阶段 1 基线 | 阶段 4 两遍识别 | 变化 |
|---|---:|---:|---:|
| CER（macro） | 16.60% | 7.69% | 相对下降 53.7% |
| 热词召回率 | 51.72% | 72.41% | +20.69 个百分点 |
| 术语误识率 | 48.28% | 27.59% | -20.69 个百分点 |
| 问题级准确率 | 39.39% | 51.52% | +12.13 个百分点 |
| 首个 partial P50 / P95 | 573 / 1015 ms | 571 / 1048 ms | 基本持平 |
| 说完后 final P50 / P95 | 旧口径 1150 / 1209 ms | 175 / 209 ms | 改为显式句末边界后测量 |
| 第二遍 CPU P50 / P95 | — | 137 / 157 ms | 低于 2500 ms 超时线 |

final 延迟从显式 `utterance_end` 到最终文本产生计算，包含第二遍整句解码。阶段 1 的 final 数据受旧文件关闭时机影响，只保留作历史参考，不把两种口径混为同一性能结论。

## 工程行为

- VAD 在正常句末和音频源关闭时发出明确的 `utterance_end`，不依赖隐式断流猜测句界。
- partial 继续用于界面即时显示；同一 `utterance_id` 的 final 平滑替换 partial。
- 第二遍改变了可见文字时，界面显示“已校正”，并可通过悬浮提示查看第一遍文本。
- 配置项为 `asr.engine`、`asr.two_pass.enabled`、`asr.two_pass.engine`、`asr.two_pass.timeout_ms` 和 `asr.hotword_boost`。
- 模型在启动采集前预热，并按音频通道复用；不会在每次停止/重启采集时重复加载。
- 第二遍不可用、抛错或超时会回退到 sherpa final；单元测试覆盖成功路径和失败回退。
- 日志只记录类型、utterance ID 和耗时，不记录原始音频或转写正文。

## 代价与未完成项

- `面试猴.spec` 增加 SenseVoice 模型，今后重新打包时模型数据会增加约 239.5 MB；本阶段没有生成 EXE。
- 独立模型加载基准为约 6.27 秒；在模型文件已进入系统缓存后的真实运行时检查中，首次启动为 3.98 秒、同进程第二次启动为 1.4 ms。运行期间会复用模型，不会在每次停止/重启采集时重复加载。
- 热词召回率 72.41%，尚未达到 90%；主要残留包括“先验框”“YOLOv8”“Transformer”“BatchNorm”“RoI Align”等。
- FunASR 与 faster-whisper 尚未做同机对比，因为本机没有相应包和模型。本阶段遵守不擅自下载大模型的边界。

## 复现

```powershell
cd E:\面试猴\面试猴-app
.\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py --two-pass `
  --report eval\asr\reports\phase4_two_pass_realtime.md
.\.venv-pack\Scripts\python.exe -X utf8 -m pytest -q
```

详细逐条结果见 `phase4_two_pass_realtime.md` 和对应 JSON。

## 验证记录

- 自动化测试：`87 passed`。
- 真实系统环回启动/停止：两次均成功进入 `capturing / connected`，停止后回到 `stopped`，队列丢帧为 0。
- 首次启动（模型预热后才开始采集）：3981.2 ms；同进程再次启动：1.4 ms，证明缓存复用生效。
- 未执行 PyInstaller，未生成新的 `面试猴.exe`。
