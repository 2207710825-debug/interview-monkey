# ASR 阶段 3：音频前端质量报告

## 结论

生产采集已统一为 16 kHz、单声道、PCM16；重采样只使用
`scipy.signal.resample_poly` 抗混叠多相滤波，不再使用线性插值兜底。
WASAPI 音频回调只负责把原始帧写入有界队列，声道平均、重采样和后续处理均在工作线程执行。

## 实现项

- 立体声/多声道使用各声道平均，未只取单侧。
- 增加轻量平滑 AGC，目标 RMS -20 dBFS、峰值限幅 -1 dBFS、最大增益 12 dB。
- 麦克风 AGC 默认开启；环回 AGC 默认关闭，均可通过 `config/config.yaml` 调整。
- 增加显式 150 ms post-roll；原有 200 ms pre-roll 保持不变。
- 增加原始帧、已处理帧、丢帧、回调状态、回调异常、工作线程异常、回调延迟、队列峰值计数。
- `/api/status` 在采集中和停止后均保留 `audio_stats`。

## AGC A/B

同一组 33 条评测音频、同一流式模型快速回放：

| 指标 | AGC 关闭（当前环回默认） | AGC 开启 | 变化 |
|---|---:|---:|---:|
| CER | 16.60% | 15.33% | -1.27 个百分点 |
| 热词召回率 | 51.72% | 53.45% | +1.73 个百分点 |
| 术语误识率 | 48.28% | 46.55% | -1.73 个百分点 |
| 问题级准确率 | 39.39% | 36.36% | -3.03 个百分点 |

因为问题级准确率下降，环回 AGC 不作为默认值。麦克风 AGC 缺少真人麦克风样本，
当前只证明了电平处理与限幅行为正确，尚不能宣称提升识别率。

## 真实设备探针

设备：48 kHz、2 声道 `spacedesk virtual Speaker [Loopback]`。

重采样器首次加载放在收到首帧之后时，40 帧均处理但队列峰值为 39；改为启动音频流前预热后：

```text
raw_frames: 40
processed_frames: 40
dropped_frames: 0
callback_status_count: 0
callback_error_count: 0
worker_error_count: 0
callback_late_count: 0
max_callback_gap_ms: 21.85
max_queue_depth: 1
```

探针期间系统没有播放声音，因此只验证了设备格式、线程、回调与计数器，没有把静音结果当作识别质量证据。

## 自动化验证

- 音频前端专项测试：25 passed
- 完整 `tests/`：83 passed
- 真实 WASAPI 设备探针：PASS

## 未启用的能力

当前打包环境没有 RNNoise、DeepFilterNet 或其模型，也没有真人麦克风评测集。
因此本阶段没有引入未经评测的强降噪依赖；环回声道保持不降噪，避免损伤辅音。
后续只有在补齐真人噪声样本并证明 CER/问题级准确率不下降后，才应增加麦克风降噪适配器。
