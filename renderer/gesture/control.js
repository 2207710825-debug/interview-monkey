import { FilesetResolver, HandLandmarker } from "./vendor/vision_bundle.mjs";
import { FistHold, classifyGesture } from "./gesture.js";

const toggleButton = document.getElementById("btnGestureControl");
const statusText = document.getElementById("gestureStatus");
const video = document.createElement("video");
video.className = "gesture-video-source";
video.autoplay = true;
video.muted = true;
video.playsInline = true;
video.setAttribute("aria-hidden", "true");
document.body.append(video);

const FIST_HOLD_MS = 800;
const HAND_LOST_MS = 900;
const FRAME_INTERVAL_MS = 100;
const CAMERA_STALL_MS = 2500;
let stream = null;
let landmarker = null;
let fistHold = new FistHold(FIST_HOLD_MS, 300);
let running = false;
let timer = null;
let lastVideoTime = -1;
let lastFrameAt = 0;
let noHandSince = null;
let minimizedByGesture = false;

function setStatus(message, active = running, error = false, detail = message) {
  statusText.textContent = message;
  statusText.title = detail;
  statusText.dataset.active = String(active);
  statusText.dataset.error = String(error);
}

function getWindowApi() {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.minimize_window || !api.restore_window) {
    throw new Error("请在面试猴桌面程序中使用手势控制");
  }
  return api;
}

async function createLandmarker() {
  const wasmRoot = new URL("./wasm/", import.meta.url).href;
  const modelPath = new URL("./models/hand_landmarker.task", import.meta.url).href;
  const fileset = await FilesetResolver.forVisionTasks(wasmRoot);
  const common = {
    runningMode: "VIDEO",
    numHands: 1,
    minHandDetectionConfidence: 0.6,
    minTrackingConfidence: 0.6,
  };

  try {
    landmarker = await HandLandmarker.createFromOptions(fileset, {
      ...common,
      baseOptions: { modelAssetPath: modelPath, delegate: "GPU" },
    });
  } catch {
    landmarker = await HandLandmarker.createFromOptions(fileset, {
      ...common,
      baseOptions: { modelAssetPath: modelPath, delegate: "CPU" },
    });
  }
}

async function restoreAfterGesture() {
  const restored = await getWindowApi().restore_window();
  if (restored) {
    minimizedByGesture = false;
    noHandSince = null;
    setStatus("窗口已恢复，手势监测继续中");
  } else {
    setStatus("恢复窗口失败，可按 Ctrl+Shift+↑ 恢复", true, true);
  }
}

async function processFrame() {
  if (!running) return;
  try {
    const now = performance.now();
    if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
        video.currentTime !== lastVideoTime) {
      lastVideoTime = video.currentTime;
      lastFrameAt = now;
      const result = landmarker.detectForVideo(video, now);
      const hand = result.landmarks && result.landmarks[0];
      const gesture = hand ? classifyGesture(hand) : "none";
      const state = fistHold.update(gesture === "fist", now);

      if (hand) {
        noHandSince = null;
      } else if (noHandSince === null) {
        noHandSince = now;
      }

      if (minimizedByGesture && noHandSince !== null &&
          now - noHandSince >= HAND_LOST_MS) {
        await restoreAfterGesture();
      }

      if (state.triggered && !minimizedByGesture) {
        const minimized = await getWindowApi().minimize_window();
        if (minimized) {
          minimizedByGesture = true;
          noHandSince = null;
          setStatus("窗口已最小化，手离开镜头后自动恢复");
        } else {
          setStatus("最小化窗口失败", true, true);
        }
      }
    } else if (minimizedByGesture && lastFrameAt &&
               now - lastFrameAt >= CAMERA_STALL_MS) {
      // 摄像头停帧时恢复窗口并结束采集，避免失效流继续占用设备。
      await stopGestureControl("摄像头画面中断，已恢复窗口并释放摄像头", true);
      return;
    }
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    await stopGestureControl(`手势识别已停止：${message}`, true);
    return;
  }

  if (running) timer = setTimeout(processFrame, FRAME_INTERVAL_MS);
}

async function startGestureControl() {
  toggleButton.disabled = true;
  setStatus("正在加载本地手部模型…", false);
  try {
    getWindowApi();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("当前 WebView 不支持摄像头，请检查 WebView2 运行时");
    }
    if (!landmarker) await createLandmarker();

    setStatus("等待摄像头授权…", false);
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
    });
    video.srcObject = stream;
    await video.play();

    fistHold = new FistHold(FIST_HOLD_MS, 300);
    lastVideoTime = -1;
    lastFrameAt = performance.now();
    noHandSince = null;
    minimizedByGesture = false;
    running = true;
    for (const track of stream.getVideoTracks()) {
      track.addEventListener("ended", () => {
        if (running) stopGestureControl("摄像头连接中断，手势控制已停止并释放摄像头", true);
      }, { once: true });
    }
    toggleButton.textContent = "✋";
    toggleButton.title = "关闭手势控制";
    toggleButton.setAttribute("aria-pressed", "true");
    toggleButton.classList.add("on");
    setStatus("监测中：握拳最小化，手离开后恢复");
    processFrame();
  } catch (error) {
    const name = error && error.name;
    let message;
    let detail;
    if (name === "NotAllowedError") {
      message = "摄像头权限未允许";
      detail = "请在 Windows 设置中允许桌面应用访问摄像头，再重试手势控制。";
    } else if (name === "NotReadableError" || name === "AbortError") {
      message = "摄像头被占用或无法共享";
      detail = "可能有其他软件独占摄像头。Windows 11 24H2 及以上可在“设置 > 蓝牙和设备 > 摄像头 > 选择摄像头 > 相机高级选项”开启“允许多个应用同时使用摄像头”。若设备不支持共享，请先关闭其他软件的摄像头，或关闭手势控制释放本应用的摄像头。";
    } else {
      message = `启动失败：${error && error.message ? error.message : String(error)}`;
      detail = message;
    }
    await stopGestureControl(message, true, detail);
  } finally {
    toggleButton.disabled = false;
  }
}

async function stopGestureControl(message = "手势控制已关闭，摄像头已释放", isError = false, detail = message) {
  running = false;
  if (timer !== null) clearTimeout(timer);
  timer = null;

  if (minimizedByGesture) {
    try {
      await getWindowApi().restore_window();
    } catch {}
  }
  minimizedByGesture = false;
  noHandSince = null;

  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  video.srcObject = null;
  if (landmarker) {
    landmarker.close();
    landmarker = null;
  }
  toggleButton.textContent = "✋";
    toggleButton.title = "开启手势控制";
  toggleButton.setAttribute("aria-pressed", "false");
  toggleButton.classList.remove("on");
  setStatus(message, false, isError, detail);
}

toggleButton.addEventListener("click", () => {
  if (running) stopGestureControl();
  else startGestureControl();
});

if (!window.pywebview || !window.pywebview.api) {
  toggleButton.disabled = true;
  window.addEventListener("pywebviewready", () => { toggleButton.disabled = false; }, { once: true });
}

// 手动快捷键或任务栏恢复窗口后，交还控制权，避免下一次丢手事件重复拉起窗口。
window.addEventListener("focus", () => {
  if (minimizedByGesture) {
    minimizedByGesture = false;
    noHandSince = null;
    if (running) setStatus("窗口已恢复，手势监测继续中");
  }
});

window.addEventListener("pagehide", () => {
  running = false;
  if (timer !== null) clearTimeout(timer);
  timer = null;
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  if (landmarker) landmarker.close();
  landmarker = null;
});
