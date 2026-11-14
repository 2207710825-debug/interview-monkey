// 面试猴 · 主进程
// 架构参考逆向学习：面试狗同样采用 Electron 主进程负责窗口 + IPC + 本地持久化。
// 面试猴的主进程职责：创建窗口、提供会议数据的 IPC 通道、把会议记录存成本地 JSON 文件（跨会话持久化）。
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("path");
const fs = require("fs");

let mainWindow = null;

// 数据文件：存放在系统用户数据目录下，纯本地、不上传
function dataFile() {
  return path.join(app.getPath("userData"), "meetings.json");
}

function readData() {
  try {
    return JSON.parse(fs.readFileSync(dataFile(), "utf8"));
  } catch (e) {
    return { sessions: [] };
  }
}

function writeData(d) {
  fs.mkdirSync(path.dirname(dataFile()), { recursive: true });
  fs.writeFileSync(dataFile(), JSON.stringify(d, null, 2), "utf8");
}

// Electron 在 Windows 上会把该调用映射为 WDA_EXCLUDEFROMCAPTURE；旧系统自动按
// WDA_MONITOR 处理。显示、恢复和跨屏移动后重申，窗口重建则由 createWindow 重新安装。
function installContentProtection(window) {
  if (process.platform !== "win32") return;

  let reapplyTimer = null;
  const apply = () => {
    if (window.isDestroyed()) return;
    window.setContentProtection(true);
    if (!window.isContentProtected()) {
      console.error("[DLP] Electron 窗口捕获保护未能生效");
    }
  };
  const scheduleApply = () => {
    if (reapplyTimer) clearTimeout(reapplyTimer);
    reapplyTimer = setTimeout(apply, 150);
  };

  apply();
  window.on("show", apply);
  window.on("restore", apply);
  window.on("moved", scheduleApply);
  window.on("closed", () => {
    if (reapplyTimer) clearTimeout(reapplyTimer);
    reapplyTimer = null;
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 740,
    minWidth: 780,
    minHeight: 540,
    title: "面试猴 · 会议实时总结助手",
    backgroundColor: "#eef1f5",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  installContentProtection(mainWindow);

  mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"));

  // 外部链接用系统浏览器打开，不跳走
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) shell.openExternal(url);
    return { action: "deny" };
  });
}

/* ===== IPC 通道（会议记录） ===== */
ipcMain.handle("meeting:list", () => readData().sessions);

ipcMain.handle("meeting:save", (_e, session) => {
  const d = readData();
  d.sessions = d.sessions.filter((s) => s.id !== session.id);
  d.sessions.push(session);
  writeData(d);
  return true;
});

ipcMain.handle("meeting:load", (_e, id) => {
  const d = readData();
  return d.sessions.find((s) => s.id === id) || null;
});

ipcMain.handle("meeting:clear", () => {
  writeData({ sessions: [] });
  return true;
});

/* ===== 生命周期 ===== */
app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
