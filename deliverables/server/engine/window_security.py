# -*- coding: utf-8 -*-
"""Windows 顶层窗口捕获保护。

直接调用 user32 的公开 Display Affinity API，不依赖额外 DLL。pywebview 窗口
重建、恢复或跨屏后会重申策略，后台线程定期回查并修复被清除的属性。
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional


WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011

ERROR_SUCCESS = 0
ERROR_INVALID_WINDOW_HANDLE = 1400
ERROR_ACCESS_DENIED = 5
ERROR_NOT_SUPPORTED = 50
ERROR_GEN_FAILURE = 31

WS_CHILD = 0x40000000
GWL_STYLE = -16
GA_ROOT = 2


@dataclass(frozen=True)
class ProtectionResult:
    ok: bool
    status: str
    hwnd: int = 0
    requested_affinity: int = WDA_EXCLUDEFROMCAPTURE
    effective_affinity: int = WDA_NONE
    preferred_error: int = ERROR_SUCCESS
    win32_error: int = ERROR_SUCCESS
    hresult: int = ERROR_SUCCESS

    def describe(self) -> str:
        return (
            "%s hwnd=0x%X requested=0x%X effective=0x%X "
            "preferred_error=%d win32_error=%d hresult=0x%08X"
            % (
                self.status,
                self.hwnd,
                self.requested_affinity,
                self.effective_affinity,
                self.preferred_error,
                self.win32_error,
                self.hresult & 0xFFFFFFFF,
            )
        )


class _Win32AffinityApi:
    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongW.restype = ctypes.c_long
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        self.user32.GetWindowDisplayAffinity.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
        self.dwmapi.DwmIsCompositionEnabled.argtypes = [ctypes.POINTER(wintypes.BOOL)]
        self.dwmapi.DwmIsCompositionEnabled.restype = ctypes.c_long

        self.enum_proc_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )
        self.user32.EnumWindows.argtypes = [self.enum_proc_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL

    @staticmethod
    def _last_error() -> int:
        return ctypes.get_last_error() or ERROR_GEN_FAILURE

    def is_own_top_level_window(self, hwnd: int) -> ProtectionResult:
        if not hwnd or not self.user32.IsWindow(hwnd):
            return ProtectionResult(False, "invalid-window", hwnd, win32_error=ERROR_INVALID_WINDOW_HANDLE)

        process_id = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value != os.getpid():
            return ProtectionResult(False, "foreign-process-window", hwnd, win32_error=ERROR_ACCESS_DENIED)

        style = self.user32.GetWindowLongW(hwnd, GWL_STYLE)
        if style & WS_CHILD or int(self.user32.GetAncestor(hwnd, GA_ROOT) or 0) != hwnd:
            return ProtectionResult(False, "not-top-level-window", hwnd, win32_error=ERROR_INVALID_WINDOW_HANDLE)
        return ProtectionResult(True, "valid-window", hwnd)

    def find_window(self, exact_title: str) -> int:
        found = []

        @self.enum_proc_type
        def callback(hwnd, _lparam):
            candidate = int(hwnd or 0)
            if not self.is_own_top_level_window(candidate).ok:
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
            if buffer.value == exact_title:
                found.append(candidate)
                return False
            return True

        self.user32.EnumWindows(callback, 0)
        return found[0] if found else 0

    def query(self, hwnd: int) -> ProtectionResult:
        validation = self.is_own_top_level_window(hwnd)
        if not validation.ok:
            return validation

        affinity = wintypes.DWORD(WDA_NONE)
        ctypes.set_last_error(ERROR_SUCCESS)
        if not self.user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
            return ProtectionResult(False, "query-failed", hwnd, win32_error=self._last_error())

        value = affinity.value
        return ProtectionResult(
            value in (WDA_NONE, WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE),
            "query-ok",
            hwnd,
            requested_affinity=value,
            effective_affinity=value,
            win32_error=ERROR_SUCCESS if value in (WDA_NONE, WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE)
            else ERROR_GEN_FAILURE,
        )

    def _set(self, hwnd: int, affinity: int) -> int:
        ctypes.set_last_error(ERROR_SUCCESS)
        if self.user32.SetWindowDisplayAffinity(hwnd, affinity):
            return ERROR_SUCCESS
        return self._last_error()

    def apply(self, hwnd: int) -> ProtectionResult:
        validation = self.is_own_top_level_window(hwnd)
        if not validation.ok:
            return validation

        composition_enabled = wintypes.BOOL()
        hresult = int(self.dwmapi.DwmIsCompositionEnabled(ctypes.byref(composition_enabled)))
        if hresult < 0 or not composition_enabled.value:
            return ProtectionResult(
                False,
                "dwm-unavailable",
                hwnd,
                win32_error=ERROR_SUCCESS if hresult < 0 else ERROR_NOT_SUPPORTED,
                hresult=hresult,
            )

        preferred_error = self._set(hwnd, WDA_EXCLUDEFROMCAPTURE)
        if preferred_error == ERROR_SUCCESS:
            current = self.query(hwnd)
            if current.ok and current.effective_affinity in (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR):
                return ProtectionResult(
                    True,
                    "protected" if current.effective_affinity == WDA_EXCLUDEFROMCAPTURE else "protected-fallback",
                    hwnd,
                    effective_affinity=current.effective_affinity,
                )
            if not current.ok:
                return current
            preferred_error = ERROR_GEN_FAILURE

        fallback_error = self._set(hwnd, WDA_MONITOR)
        if fallback_error != ERROR_SUCCESS:
            return ProtectionResult(
                False,
                "set-failed",
                hwnd,
                preferred_error=preferred_error,
                win32_error=fallback_error,
            )

        current = self.query(hwnd)
        if not current.ok:
            return ProtectionResult(
                False,
                current.status,
                hwnd,
                preferred_error=preferred_error,
                win32_error=current.win32_error,
            )
        if current.effective_affinity != WDA_MONITOR:
            return ProtectionResult(
                False,
                "verification-failed",
                hwnd,
                effective_affinity=current.effective_affinity,
                preferred_error=preferred_error,
                win32_error=ERROR_GEN_FAILURE,
            )
        return ProtectionResult(
            True,
            "protected-fallback",
            hwnd,
            effective_affinity=WDA_MONITOR,
            preferred_error=preferred_error,
        )


class WindowSecurityGuard:
    """绑定一个 pywebview 顶层窗口，并持续维护 Display Affinity。"""

    def __init__(
        self,
        exact_title: str,
        logger: Optional[Callable[[str], None]] = None,
        audit_interval: float = 2.0,
    ) -> None:
        self.exact_title = exact_title
        self.logger = logger or (lambda _message: None)
        self.audit_interval = max(0.5, float(audit_interval))
        self._api = _Win32AffinityApi() if os.name == "nt" else None
        self._window = None
        self._hwnd = 0
        self._effective_affinity = WDA_NONE
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._last_report = None

    def bind_pywebview(self, window) -> None:
        self._window = window
        events = window.events
        subscriptions = (
            ("before_show", self._on_before_show),
            ("shown", self._on_lifecycle_change),
            ("restored", self._on_lifecycle_change),
            ("restore", self._on_lifecycle_change),
            ("moved", self._on_lifecycle_change),
            ("closed", self._on_closed),
        )
        for name, handler in subscriptions:
            event = getattr(events, name, None)
            if event is not None:
                event += handler
        self._start_audit_thread()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(1.0, self.audit_interval))

    def _native_hwnd(self) -> int:
        native = getattr(self._window, "native", None)
        handle = getattr(native, "Handle", None)
        for value in (handle, native):
            try:
                if hasattr(value, "ToInt64"):
                    candidate = int(value.ToInt64())
                else:
                    candidate = int(value)
                if self._api.is_own_top_level_window(candidate).ok:
                    return candidate
            except (TypeError, ValueError, OverflowError, AttributeError):
                continue
        return self._api.find_window(self.exact_title)

    def _protect(self, force: bool) -> ProtectionResult:
        if self._api is None:
            return ProtectionResult(False, "unsupported-platform", win32_error=ERROR_NOT_SUPPORTED)

        with self._lock:
            hwnd = self._native_hwnd()
            if not hwnd:
                result = ProtectionResult(False, "window-not-found", win32_error=ERROR_INVALID_WINDOW_HANDLE)
                self._report(result)
                return result

            if not force and hwnd == self._hwnd:
                current = self._api.query(hwnd)
                if current.ok and current.effective_affinity == self._effective_affinity != WDA_NONE:
                    return current

            result = self._api.apply(hwnd)
            if result.ok:
                self._hwnd = hwnd
                self._effective_affinity = result.effective_affinity
            self._report(result)
            return result

    def _report(self, result: ProtectionResult) -> None:
        signature = (
            result.ok,
            result.status,
            result.hwnd,
            result.effective_affinity,
            result.preferred_error,
            result.win32_error,
            result.hresult,
        )
        if signature != self._last_report:
            prefix = "[DLP] 窗口保护已生效" if result.ok else "[DLP] 窗口保护失败"
            self.logger("%s: %s" % (prefix, result.describe()))
            self._last_report = signature

    def _start_audit_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._audit_loop,
            name="window-security-audit",
            daemon=True,
        )
        self._thread.start()

    def _audit_loop(self) -> None:
        while not self._stop.wait(self.audit_interval):
            self._protect(force=False)

    def _on_before_show(self, window=None, *_args) -> None:
        if window is not None:
            self._window = window
        self._protect(force=True)

    def _on_lifecycle_change(self, *_args) -> None:
        self._protect(force=True)

    def _on_closed(self, *_args) -> None:
        self.stop()
