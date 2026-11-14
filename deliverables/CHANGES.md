# 变更说明

## 改动范围

- `engine/realtime/protocol.py`：新增 36 字节 little-endian 音频头编解码、CRC32、长度、字段、序号和重复帧校验。
- `engine/realtime/mock_backend.py`：新增 fixture SSE service、fixture capture runtime、鉴权 WebSocket Mock 会话、重连恢复、故障注入和结构化 JSONL 审计。
- `engine/realtime/api/app.py`：增加 `mock=True` 工厂选项；只在 Mock 模式注册 `/v1/realtime`，默认 `/api/*` 真实流水线保持原样。
- `realtime_server.py`：增加 `--mock` CLI；参数解析前不再提前创建真实 service，服务仍绑定 `127.0.0.1`。
- `fixtures/realtime_mock.json`：固定预置转写、会议纪要、问题、回答 token 和来源。
- `contract/`：更新 v1 event schema 的音频秒数统计，并在握手拒绝/双 `meta` 等实测差异处贴合运行行为。
- `tests/`：新增协议测试、100ms PCM 模拟客户端和旧 SSE 前端对齐检查。
- `deliverables/`：契约、Schema、源码快照、测试副本、终端/协议日志和报告。

## 兼容性与回滚

- 普通启动 `python realtime_server.py` 不注册 `/v1/realtime`，继续使用现有真实处理链路。
- `--mock` 下旧 SSE 事件名与字段不变；回答取 fixture 固定样例，不调用推理服务。
- 认证只覆盖 Mock v1 WebSocket，不扩展到已有 `/api/*` 路由。
- 可分别回滚 Phase 3 交付/测试提交与 Phase 2 Mock 实现提交：在功能分支上用 `git revert <commit-sha>`；Phase 0/1 提交不受影响。

## 关键差异片段

```diff
def create_app(..., *, mock: bool = False, mock_settings: MockSettings | None = None):
    if mock:
        app.state.answer_service = FixtureAnswerService(...)
        app.state.capture_runtime = FixtureCaptureRuntime(...)
        app.state.mock_protocol_server = MockProtocolServer(...)
    else:
        app.state.answer_service = service or build_default_service()
        app.state.capture_runtime = runtime or CaptureRuntime(...)
```

```diff
if mock:
    @app.websocket("/v1/realtime")
    async def realtime_v1(websocket: WebSocket):
        await app.state.mock_protocol_server.handle(websocket)
```

## Git 统计

Phase 2/3 相对 Phase 1 (`a4fbef5`) 的 `git diff --stat`，排除本说明文件和承载该统计的日志文件，完整输出见 `logs/git-diff-stat.txt`：

```text
138 files changed, 52635 insertions(+), 32 deletions(-)
```

统计包含按要求放入交付包的源码快照、原 `tests/` 全部文件副本和实际运行 JSONL 日志；不是只统计运行时代码改动。
