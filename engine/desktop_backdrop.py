# -*- coding: utf-8 -*-
"""本机桌面背景采样（伪透明）。

Windows 上 pywebview/WebView2 的顶层窗口没有可靠的逐像素透明：页面即使
写成 transparent，窗口客户区仍会露出一块不透明底色。这里利用应用已经
设置的 ``WDA_EXCLUDEFROMCAPTURE``（窗口不进入屏幕捕获）采样窗口背后的
桌面，把采样图作为页面背景。这样看起来就是真正的玻璃透明，而且不会把
应用自身拍进背景造成递归。

图片只在本进程内存和页面 DOM 中出现，不落盘、不上传。窗口移动、缩放
或背后的窗口变化时，下一次采样会自动更新。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import threading
import time
from ctypes import wintypes
from typing import Optional


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class DesktopBackdrop:
    """按固定间隔采样窗口背后的桌面并更新页面背景。"""

    def __init__(self, window, guard=None, interval: float = 0.8, logger=None) -> None:
        self.window = window
        self.guard = guard
        self.interval = max(0.25, float(interval))
        self.logger = logger or (lambda _message: None)
        self._reported_unprotected = False
        self._reported_capture_error = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_hash = b""
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
        self._user32.GetClientRect.restype = wintypes.BOOL
        self._user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_Point)]
        self._user32.ClientToScreen.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="desktop-backdrop",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _hwnd(self) -> int:
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return 0

    def _guard_ready(self) -> bool:
        if self.guard is None:
            return False
        return bool(getattr(self.guard, "capture_excluded", False))

    def _capture(self, hwnd: int) -> Optional[tuple[int, int, bytes]]:
        from System.Drawing import Bitmap, Graphics, Size
        from System.Drawing.Imaging import ImageFormat
        from System.IO import MemoryStream

        if self._user32.IsIconic(hwnd):
            return None
        rect = _Rect()
        if not self._user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < 32 or height < 32:
            return None
        origin = _Point(0, 0)
        if not self._user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None

        bitmap = graphics = stream = None
        try:
            bitmap = Bitmap(width, height)
            graphics = Graphics.FromImage(bitmap)
            graphics.CopyFromScreen(
                int(origin.x), int(origin.y), 0, 0, Size(width, height)
            )
            stream = MemoryStream()
            bitmap.Save(stream, ImageFormat.Jpeg)
            raw = bytes(stream.ToArray())
            return width, height, raw
        except Exception:
            return None
        finally:
            for value in (stream, graphics, bitmap):
                try:
                    if value is not None:
                        value.Dispose()
                except Exception:
                    pass

    def _run(self) -> None:
        # 等待窗口保护生效；只有窗口被排除在屏幕捕获之外，采样才不会
        # 把应用自身拍进背景。保护失败时宁可保持原样，也不做递归采样。
        deadline = time.monotonic() + 8.0
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._guard_ready():
                break
            time.sleep(0.2)
        if not self._guard_ready():
            self.logger("[backdrop] 窗口未被排除在屏幕捕获之外，跳过桌面采样")
            return
        self.logger("[backdrop] 桌面背景采样已启动")

        while not self._stop.is_set():
            if not self._guard_ready():
                if not self._reported_unprotected:
                    self.logger("[backdrop] 窗口保护暂时失效，暂停采样")
                    self._reported_unprotected = True
                self._stop.wait(self.interval)
                continue
            self._reported_unprotected = False
            hwnd = self._hwnd()
            if hwnd:
                captured = self._capture(hwnd)
                if captured is None and not self._reported_capture_error:
                    self.logger("[backdrop] 桌面采样失败（将保持原背景）")
                    self._reported_capture_error = True
                if captured:
                    _width, _height, raw = captured
                    digest = hashlib.sha1(raw).digest()
                    if digest != self._last_hash:
                        self._last_hash = digest
                        encoded = base64.b64encode(raw).decode("ascii")
                        script = (
                            "(()=>{const h=document.documentElement;"
                            "h.style.backgroundImage='url(data:image/jpeg;base64," + encoded + ")';"
                            "h.style.backgroundSize='100% 100%';"
                            "h.style.backgroundRepeat='no-repeat';"
                            "h.style.backgroundPosition='top left';})()"
                        )
                        try:
                            self.window.evaluate_js(script)
                        except Exception:
                            return
            self._stop.wait(self.interval)
