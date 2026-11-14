# -*- coding: utf-8 -*-
"""用原生 COM vtable 实现 CreateCoreWebView2EnvironmentCompletedHandler，
直接调 loader 的 C API 创建环境。成功即证明运行时完好。"""
import os, ctypes, tempfile, time
from ctypes import wintypes, c_void_p, c_long, c_ulong, POINTER, Structure, WINFUNCTYPE

LOADER = r"E:\面试猴\.workbuddy\gui-build\venv\Lib\site-packages\webview\lib\runtimes\win-x64\native\WebView2Loader.dll"
dll = ctypes.WinDLL(LOADER)
ole32 = ctypes.WinDLL("ole32")
user32 = ctypes.WinDLL("user32")

hr = ole32.CoInitializeEx(None, 0x2)   # STA
print(f"CoInitializeEx(STA)=0x{hr & 0xFFFFFFFF:08X}", flush=True)

PROFILE = os.path.join(tempfile.gettempdir(), "wv_vtable")
os.makedirs(PROFILE, exist_ok=True)

STATE = {"done": False, "hr": None, "env": None}

# ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler 的 Invoke 签名：
# HRESULT Invoke(THIS, HRESULT errorCode, ICoreWebView2Environment* createdEnvironment)
INVOKE = WINFUNCTYPE(c_long, c_void_p, c_long, c_void_p)

def _invoke(this, error_code, env_ptr):
    STATE["done"] = True
    STATE["hr"] = error_code & 0xFFFFFFFF
    STATE["env"] = env_ptr
    return 0

_invoke_cb = INVOKE(_invoke)

class VTBL(Structure):
    _fields_ = [
        ("QueryInterface", c_void_p),
        ("AddRef", c_void_p),
        ("Release", c_void_p),
        ("Invoke", c_void_p),
    ]

class Handler(Structure):
    _fields_ = [("vtbl", POINTER(VTBL))]

# 用全局保持引用
_QI = WINFUNCTYPE(c_long, c_void_p, c_void_p, c_void_p)(lambda t, a, b: -2147467262)  # E_NOINTERFACE
_ADDREF = WINFUNCTYPE(c_ulong, c_void_p)(lambda t: 2)
_REL = WINFUNCTYPE(c_ulong, c_void_p)(lambda t: 1)

_vtbl = VTBL(
    ctypes.cast(_QI, c_void_p),
    ctypes.cast(_ADDREF, c_void_p),
    ctypes.cast(_REL, c_void_p),
    ctypes.cast(_invoke_cb, c_void_p),
)
_obj = Handler(ctypes.pointer(_vtbl))

dll.CreateCoreWebView2EnvironmentWithOptions.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, c_void_p, POINTER(Handler)
]
dll.CreateCoreWebView2EnvironmentWithOptions.restype = c_long

hr = dll.CreateCoreWebView2EnvironmentWithOptions(None, PROFILE, None, ctypes.byref(_obj))
print(f"CreateCoreWebView2EnvironmentWithOptions=0x{hr & 0xFFFFFFFF:08X}", flush=True)

if hr == 0:
    msg = wintypes.MSG()
    t0 = time.time()
    while not STATE["done"] and time.time() - t0 < 25:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)

if STATE["done"]:
    print(f"回调 errorCode=0x{STATE['hr']:08X}  env={STATE['env']}", flush=True)
    if STATE["hr"] == 0:
        print(">>> 原生 API 创建环境成功：WebView2 运行时完全正常", flush=True)
        print(">>> 因此故障在 pywebview/.NET 包装层", flush=True)
else:
    print("回调未触发（超时）", flush=True)
