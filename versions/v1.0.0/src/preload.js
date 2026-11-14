// 面试猴 · 预加载脚本
// 与逆向学到的面试狗 preload 思路一致：通过 contextBridge 把受控 API 暴露给渲染进程，
// 渲染进程不直接接触 Node/文件系统，保证隔离安全。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("meetingStore", {
  list: () => ipcRenderer.invoke("meeting:list"),
  save: (session) => ipcRenderer.invoke("meeting:save", session),
  load: (id) => ipcRenderer.invoke("meeting:load", id),
  clear: () => ipcRenderer.invoke("meeting:clear"),
});

// 供渲染进程判断当前是否运行在 Electron 内
contextBridge.exposeInMainWorld("isElectron", true);
