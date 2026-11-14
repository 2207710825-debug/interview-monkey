# 面试猴 · 会议实时总结助手（Electron 版）

全语音 · 零门槛 · 会议实时转写 + 自动纪要 + 疑问解答，数据只存本地。

## 一、先逆向 → 再正向（本项目的由来）

本项目通过对「面试狗」的 REA 逆向分析，摸清了它的 Electron 三层架构，然后正向复用了这套**通用技术骨架**，实现功能正当的「面试猴」：

| 逆向学到的技术 | 面试猴中的正向复用 |
|---|---|
| Electron 主进程负责窗口 + IPC | `src/main.js` 负责窗口 + IPC 通道 |
| preload 用 contextBridge 暴露受控 API | `src/preload.js` 暴露 `meetingStore`（隔离安全） |
| 渲染端 React/Vite 单页 | `renderer/index.html` 单页（原生 JS，零依赖） |
| 音频采集 → 实时转写 → 分析管线 | Web Speech API 实时转写 → 本地规则分析 |
| 本地持久化（面试狗用 SQLite） | 会议记录存本地 JSON（`userData/meetings.json`） |

**刻意不复刻的部分**：实时面试代答、虚拟摄像头换脸、四平台自动投简历——这些是作弊/欺诈/破坏平台规则，不在本项目范围内。

## 二、目录结构

```
面试猴-app/
├─ package.json          # Electron 工程配置
├─ src/
│  ├─ main.js            # 主进程：窗口 + IPC + 本地存储
│  └─ preload.js         # 预加载：contextBridge 暴露 API
└─ renderer/
   └─ index.html         # 渲染端：会议助手 UI（含 CSS/JS）
```

## 三、功能

- 🎙 实时录音转文字（Web Speech API，Chrome/Edge 内核可用）
- 🔑 实时提取：高频关键词、数字/金额/时间、已确认结论、待办事项
- ❓ 疑问解答：对会议内容提问，本地关键词检索返回相关片段
- 📋 自动生成会议纪要，可复制 / 下载 txt
- 🕘 历史记录持久化到本地文件

## 四、运行

```bash
cd 面试猴-app
npm install          # 首次安装 Electron
npm start            # 启动应用
```

> 语音转文字需要 Chrome/Edge 内核（Electron 自带 Chromium，支持）。
> 若需「系统声音（对方说话）也转写」，当前版本先支持麦克风，系统音频回环可在后续接入。

## 五、Roadmap

- [ ] 系统音频回环采集（会议中对方的声音）
- [ ] 说话人区分（diarization）
- [ ] 接入可选 LLM：段落式纪要 + 跨句追问
- [ ] 打包成 Windows 安装包（electron-builder）
