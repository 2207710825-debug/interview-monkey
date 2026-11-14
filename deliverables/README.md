# 本地 Mock 服务与联调包

本目录保存 Phase 2/3 的接口契约副本、Schema、测试脚本、运行日志与变更报告。可执行源码以仓库根目录为准；`server/` 是本次改动文件的审阅快照。Mock 依赖完整项目中的 `engine/` 模块，因此请在项目根目录启动。

## 启动 Mock

在 PowerShell 中：

```powershell
cd E:\面试猴\面试猴-app
.\.venv-pack\Scripts\python.exe -m pip install -r deliverables\requirements.txt
$env:DEV_TOKEN = 'local-mock-token'
.\.venv-pack\Scripts\python.exe realtime_server.py --mock
```

打开 `http://127.0.0.1:18765/`。页面仍使用现有 `/api/events` SSE；转写、问题和回答内容全部取自 `fixtures/realtime_mock.json`，Mock 不运行 ASR、RAG 或 LLM。新增 `/v1/realtime` WebSocket 也只在 `--mock` 模式注册。

停止服务按 `Ctrl+C`。不设置 `DEV_TOKEN` 时，v1 WebSocket 握手会被拒绝。服务固定绑定 `127.0.0.1`。

`server/` 是可单独启动的服务快照，包含所需 Python 源码、默认本地配置、网页和 fixture。也可以在快照目录启动：

```powershell
cd E:\面试猴\面试猴-app\deliverables\server
python -m pip install -r ..\requirements.txt
$env:DEV_TOKEN = 'local-mock-token'
python realtime_server.py --mock
```

## 联调与压力测试

```powershell
# 项目测试（原有回归 + v1 Mock 联调）
.\.venv-pack\Scripts\python.exe -m pytest -q tests

# 浏览器旧 SSE 事件对齐检查，并将摘要写入 deliverables/logs/
.\.venv-pack\Scripts\python.exe tests\frontend_alignment.py --base-url http://127.0.0.1:18765

# 50 路并发，每路 30 秒、100ms PCM16 音频帧
$env:DEV_TOKEN = 'local-mock-token'
.\.venv-pack\Scripts\python.exe tests\sim_client.py --url ws://127.0.0.1:18765/v1/realtime --concurrency 50 --duration 30
```

测试客户端只生成本机正弦波 PCM，不含用户麦克风数据。交付日志位于 `deliverables/logs/`；运行时 JSONL 由服务写入项目 `logs/mock_protocol.jsonl`。

## 故障注入

在启动 `--mock` 前设置这些变量；默认不注入故障：

| 环境变量 | 行为 |
|---|---|
| `MH_MOCK_DELAY_MS=80` | 延迟每个预置 token |
| `MH_MOCK_DROP_SEQ=3` | 指定帧返回 `SEQ_GAP` |
| `MH_MOCK_DISCONNECT_AFTER_FRAMES=4` | 收到第 4 帧后断开一次，可用 resume 恢复 |
| `MH_MOCK_IGNORE_PING=1` | 忽略应用层 ping，便于验证超时 |
| `MH_MOCK_HEARTBEAT_TIMEOUT_SECONDS=1` | 缩短 Mock 心跳超时，仅用于测试 |
| `MH_MOCK_ERROR_CODE=INTERNAL` | 首个音频帧注入指定错误码 |

## 文件说明

- `API_CONTRACT.md`、`schemas/`、`examples/`：v1 WebSocket 契约及旧 SSE 契约示例。
- `server/`：可独立启动的服务端代码快照；只包含 Python 源文件，不含模型二进制和用户运行数据。
- `tests/`：原有 `tests/` 全部文件副本，含新增模拟客户端和 Mock 联调测试。
- `logs/frontend_mock_alignment.jsonl`：旧 SSE 前端事件名称、转写/问题/回答顺序摘要。
- `logs/mock_protocol.jsonl`：实际运行的结构化协议日志；不包含音频、令牌或回答正文。
- `logs/phase3_verification.txt`：命令与实测结果记录。
- `CHANGES.md`、`REPORT.md`：变更、验证结果、局限和回滚说明。
