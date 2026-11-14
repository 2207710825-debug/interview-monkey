# Phase 2/3 实施与联调报告

## 差距与结果

| 能力 | Phase 0 现状 | Phase 2/3 结果 | 兼容性/说明 |
|---|---|---|---|
| 握手鉴权 | 缺失 | Mock v1 读取本地 `DEV_TOKEN`，拒绝非回环连接；当前 Uvicorn 对缺失/错误 token 都返回 HTTP 403 | 只对 `--mock` 的 `/v1/realtime` 生效；不改旧 `/api/*` |
| 控制帧 | 缺少客户端协议 | `start/pause/resume/stop/ack/ping` 状态处理 | v1 新接口 |
| 音频帧头、seq、长度、EOF、CRC | 内部 `AudioFrame` 没有网络头 | 新增 36 字节帧；验证 magic/version/type/flags/reserved/rate/len/seq/CRC/单调时间戳；相同旧帧可幂等 ACK | 不修改既有 ASR `AudioFrame` |
| 心跳与超时 | 无客户端 WebSocket 会话 | ping/pong；默认 45 秒无有效活动后发 `HEARTBEAT_TIMEOUT` 并 close 4408 | Mock 超时可缩短用于测试 |
| 重连与恢复 | 无 resume token | 600 秒内存态 token，按 `last_event_index` 重播事件；序号续传且重复帧不重复计数 | 服务进程重启后不恢复 |
| `meta/token/done/error` | 仅旧 SSE 字段 | v1 Schema 校验的列区分 token、索引、统计和稳定错误码；保留 ack/pong | SSE 字段原样保留 |
| 故障注入 | 缺失 | delay、drop、disconnect、ignore ping、指定错误码 | 环境变量只影响 `--mock` |
| 结构化日志 | 只有既有 QA/延迟日志 | JSONL 记录状态、帧类型/长度/seq、错误原因；不记录 PCM、token 或回答正文 | `logs/mock_protocol.jsonl` |
| 会话并发隔离 | 单个 `CaptureRuntime` | 50 路并发测试互不串流；50×30 秒模拟推流通过；同一会话拒绝第二条活动恢复连接 | 本地回环压力结果，不代表生产网络 SLA |
| 前端对齐 | 旧前端通过 SSE | 浏览器中三栏实际显示 fixture 转写、问题、来源与回答；SSE 对齐检查通过 | 页面无需迁移到 v1 WebSocket |

## 验证结果

| 检查 | 结果 |
|---|---|
| Phase 0 回归基线：`python -m pytest -q tests` | 125 passed、177 subtests passed |
| Phase 2 后完整 `tests/` 回归 | 见 `logs/phase3_verification.txt` 的最终命令输出 |
| Schema 示例 | `contract/validate_contract.py` 校验 19 个 JSON 示例、5 个 Schema 通过 |
| Mock WebSocket 联调 | 覆盖握手拒绝、控制帧、正常闭环、错误头/CRC/seq、心跳、断线恢复、fixture 延迟、故障码、50 路会话隔离 |
| 50 路×30 秒长测 | 50/50 完成；15,000 帧；48,000,000 PCM 字节；ACK p50 28.139 ms、p95 67.321 ms；整轮 49.468 秒 |
| 旧 UI SSE | `transcript → question_decision → answer(meta/token/done)`，通过；浏览器可访问性树显示三栏实际内容 |
| 默认真实启动 | `GET /api/status` 返回 HTTP 200、`transport=sse`、`capture=stopped`；默认 app 未注册 `/v1/realtime` |

最终回归命令 `python -m pytest -q tests`：**136 passed、68 warnings、177 subtests passed in 17.11s**。Phase 0 的同一路径基线为 125 passed、177 subtests passed。本仓库 Phase 0 已记录根目录 `python -m pytest -q` 会在根级 `test_app_api.py` 导入时触发 `SystemExit`，该既有问题未在本任务修改；Phase 0 专用基线命令 `python -m pytest -q tests` 正常通过。

## 已知限制

- Mock 返回固定 fixture，不代表 ASR 字错率、检索质量或 LLM 答案质量；本阶段没有修改真实处理链路。
- v1 目前只随 `--mock` 提供，恢复状态只存内存，服务重启后 token 失效。
- 依赖栈的 `websockets.legacy` / Uvicorn WebSocket 适配器会输出弃用警告，但当前测试及联调仍通过。
- 当前 Uvicorn WebSocket 握手前拒绝不支持自定义 HTTP denial response；缺失与无效令牌共用 HTTP 403。认证原因在服务端 JSONL 审计中区分。

## 复现

见 `README.md` 的 PowerShell 启动、SSE 对齐和 50 路压测命令；每次测试均只监听 `127.0.0.1`，使用 `local-mock-token` 作为本地假令牌。
