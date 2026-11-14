# 工作记录

## 2026-11-14 (UTC)

### Phase 0 基线审计（此前实际运行结果）

| 命令/操作 | 结论 | 涉及范围 |
|---|---|---|
| `\.venv-pack\Scripts\python.exe -m pytest -q tests` | 通过：125 passed，177 subtests passed，25.91s | `tests/` |
| `python -m pytest -q` | 失败：pytest 收集根目录 `test_app_api.py` 时执行导入期问答并 `sys.exit(0)`，pytest 报 `INTERNALERROR` / `no tests ran` | 根目录测试脚本 |
| `python test_config_persist.py` | 通过 | `engine/config_store.py` 持久化回归 |
| `python test_stream_e2e.py` | 失败：`OllamaBackend` 缺少 `generate_stream` | `engine/llm.py`、`test_stream_e2e.py` |
| 启动 `\.venv-pack\Scripts\python.exe realtime_server.py`，检查 `GET /api/status`、`GET /api/knowledge`、`POST /api/answer/stream` | 服务绑定 `127.0.0.1:18765`；HTTP 200；SSE 顺序为 `meta`、150 个 `token`、`done`。记录到 RAG 11.257ms、TTFT 556.083ms、端到端 556.086ms | `realtime_server.py`、`engine/realtime/api/app.py` |
| 运行前检查 `test_fix_404.py`、`test_ollama_e2e.py` | 未运行在线凭据调用；后者读取用户配置。避免把在线测试或用户凭据带入基线记录 | 根目录测试脚本 |

### 本次 GitHub 版本管理准备

| UTC 时间 | 命令/操作 | 结论 | 文件 |
|---|---|---|---|
| 2026-11-14 06:58 | `gh auth status`、`gh repo view 2207710825-debug/interview-monkey` | 已认证为 `2207710825-debug`；目标仓库此前不存在，拟创建私有仓库 | GitHub 远端 |
| 2026-11-14 06:58 | `git init -b main`、`git lfs install --local`、设置仓库本地 Git 身份 | 初始化成功；Git LFS 可用；配置 GitHub noreply 邮箱 | `.git/` |
| 2026-11-14 06:59 | 检查大文件、录音和凭据形状字符串 | exe/build/dist 和本地录音不纳入；ONNX 由 LFS 管理。扫描到的凭据形状值位于测试夹具：3 个 `sk-test` 合成值和 1 个本次替换的虚构 Zhipu 格式样例，没有发现待上传的真实 Key | `.gitignore`、`.gitattributes`、`test_config_persist.py`、`test_fix_404.py` |
| 2026-11-14 06:59 | 修改配置输出与在线测试默认行为 | `test_app_api.py`、`test_ollama_e2e.py` 输出前将 `api_key` 替换为 `[REDACTED]`；`test_fix_404.py` 默认只测格式识别，联网测试须显式设置 `MH_RUN_ONLINE_TESTS=1` 与 `MH_ZHIPU_TEST_API_KEY` | `test_app_api.py`、`test_ollama_e2e.py`、`test_fix_404.py` |
| 2026-11-14 07:00 | 新增仓库忽略规则与 LFS 克隆说明 | 忽略 exe、备份、build/dist、venv、缓存、用户状态、录音和运行日志；跟踪 `*.onnx`；README 增加 LFS 克隆步骤 | `.gitignore`、`.gitattributes`、`README.md` |
| 2026-11-14 07:00 | `\.venv-pack\Scripts\python.exe -m py_compile test_app_api.py test_ollama_e2e.py test_fix_404.py` | 语法检查通过；暂存审查为 277 个文件，0 个 exe/备份/pkg，LFS 8 个唯一 ONNX 对象（历史版本路径复用相同对象） | 3 个脱敏测试脚本、Git 暂存区 |
| 2026-11-14 07:18 | `git push -u origin main`；`git ls-remote --heads origin`；`gh api repos/2207710825-debug/interview-monkey/branches` | LFS 的 8 个对象（470 MB）上传完成；Git 引用推送因 `github.com:443` 连接中断/超时而失败，远端当时没有分支。`curl` 到 `github.com` 超时，但 GitHub API 可用 | GitHub 远端、`.git/` |
| 2026-11-14 07:18 | `gh api --method PUT repos/2207710825-debug/interview-monkey/contents/README.md --input -` | 空仓库 Git Data API 返回 409 后，用内容 API 建立 README 种子提交 `6c0fb3922318cd8bbd27d4e79e35b6ff0f8d1428`；仓库确认是私有，默认分支为 `main` | GitHub 远端 `main` |
| 2026-11-14 07:23 | GitHub Git Data API 创建唯一 blob、tree、commit 和 refs | 246 个唯一 blob 均返回与本地相同 SHA；277 个文件树 SHA 相同；远端 commit 与本地 `aa821c31cad2e47082fe12c6fc77368789b0605f` 完全一致。私有仓库 `main` 与 `feat/protocol-contract-mock` 均已存在；远端 API 可读到 LFS 模型原始大小 239,233,841 字节 | GitHub 远端、`.git/` |
| 2026-11-14 07:24 | `git rev-parse HEAD^{tree}` | PowerShell 将花括号表达式解析成了错误参数；提交本身成功，后续用 `git show -s --format=%T HEAD` 读取 tree SHA | 本地 `.git/` |
| 2026-11-14 07:27 | 根据 GitHub API 返回的分支 SHA 执行 `git update-ref`、`git branch --set-upstream-to`、`git switch -c feat/protocol-contract-mock` | 本地 `main` 与功能分支均跟踪对应远端分支；当前工作区干净。因 Git HTTPS 连接不可用，远端跟踪引用根据已核验 API SHA 设置 | 本地 `.git/` |

### 安全边界与后续状态

- 仓库计划设为私有。提交前复核暂存区；确认没有 exe、venv、用户录音或真实凭据后才推送。
- `samples/loop*.wav` 是本地 loopback 录音，因此忽略；`samples/tts*.wav` 与 `eval/asr/samples/` 为合成评测样例，可纳入。
- 本阶段只建立 GitHub 远端、基线提交和功能分支；Phase 1 协议实现等待用户确认 Phase 0 差距分析。
