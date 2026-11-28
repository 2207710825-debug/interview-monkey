# -*- coding: utf-8 -*-
"""实时闭环入口。

源码运行时启动独立 FastAPI 服务；PyInstaller 冻结后则在本机随机端口启动
服务并打开 pywebview 窗口，最终仍只交付一个 ``面试猴.exe``。
"""
import os
import socket
import sys
import threading
import time
import argparse
from pathlib import Path

from engine.realtime.api.app import create_app

# 避免 `python realtime_server.py --mock` 在解析参数前先构造真实 ASR/RAG 服务。
app = None if __name__ == "__main__" else create_app()


def _run_server(app_instance) -> None:
    """开发模式：保留原有的浏览器调试入口。"""
    import uvicorn

    port = int(os.environ.get("MH_REALTIME_PORT", "18765"))
    uvicorn.run(app_instance, host="127.0.0.1", port=port, log_level="info")


def _available_port() -> int:
    """为桌面包选择空闲回环端口，避免与开发服务器冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BackgroundRealtimeServer:
    """供原桌面主界面嵌入实时问答页的后台服务。"""

    def __init__(self, app_instance=None) -> None:
        self.app = app_instance or app
        self.port = 0
        self.url = ""
        self.server = None
        self.thread = None

    def start(self, timeout: float = 15.0) -> str:
        if self.thread is not None and self.thread.is_alive() and self.url:
            return self.url

        import uvicorn

        self.port = _available_port()
        self.server = uvicorn.Server(uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        ))
        self.thread = threading.Thread(
            target=self.server.run,
            name="realtime-api",
            daemon=True,
        )
        self.thread.start()

        deadline = time.monotonic() + timeout
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            self.stop()
            raise RuntimeError("本地实时服务未能启动")

        self.url = f"http://127.0.0.1:{self.port}/"
        return self.url

    def reload_llm_config(self) -> dict:
        """设置页保存后立即把新模型推给已启动的实时服务。

        该方法由 pywebview 的 js_api 线程调用，因此不能依赖请求上下文；
        FastAPI app.state 上的 reload 回调内部有锁，并原地替换 provider。
        """
        state = getattr(self.app, "state", None)
        callback = getattr(state, "reload_llm_config", None)
        if not callable(callback):
            return {"ok": False, "changed": False, "error": "实时服务未启用动态模型配置"}
        try:
            return dict(callback(force=True))
        except Exception as exc:
            return {"ok": False, "changed": False, "error": str(exc)}

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=5.0)
        self.url = ""


def _show_startup_error(message: str) -> None:
    """无控制台打包模式下仍向用户显示可诊断的启动错误。"""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "面试猴启动失败", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _run_desktop(app_instance) -> None:
    """冻结模式：后台运行 API，并用 pywebview 承载三栏实时页面。"""
    import webview

    from engine.window_security import WindowSecurityGuard

    background = BackgroundRealtimeServer(app_instance)
    try:
        url = background.start()
    except Exception:
        _show_startup_error("本地实时服务未能启动，请检查安全软件或端口权限。")
        return

    title = "面试猴 · 实时专业知识辅助"
    window = webview.create_window(
        title,
        url,
        width=1500,
        height=900,
        min_size=(1000, 650),
    )
    guard = WindowSecurityGuard(title, audit_interval=2.0)
    guard.bind_pywebview(window)

    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    storage_dir = local_app_data / "面试猴" / "webview"
    storage_dir.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(storage_path=str(storage_dir), private_mode=False)
    except Exception as exc:
        _show_startup_error(f"桌面窗口启动失败：{exc}")
    finally:
        guard.stop()
        background.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="面试猴本机实时服务")
    parser.add_argument("--mock", action="store_true", help="使用本地预置 fixtures，不调用 ASR/RAG/LLM")
    args = parser.parse_args()
    app = create_app(mock=args.mock)
    if getattr(sys, "frozen", False):
        _run_desktop(app)
    else:
        _run_server(app)
