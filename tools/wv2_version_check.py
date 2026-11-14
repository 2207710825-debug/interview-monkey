# -*- coding: utf-8 -*-
"""排查 CreateAsync 的 0x80070002：重点测 userDataFolder 路径的影响。"""
import os, tempfile
from ctypes import wintypes

LOADER = r"E:\面试猴\.workbuddy\gui-build\venv\Lib\site-packages\webview\lib\runtimes\win-x64\native\WebView2Loader.dll"
import ctypes
dll = ctypes.WinDLL(LOADER)

dll.GetAvailableCoreWebView2BrowserVersionString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)]
dll.GetAvailableCoreWebView2BrowserVersionString.restype = ctypes.c_long

cases = [
    ("NULL(系统默认)", None),
    ("Program Files 路径", r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application"),
    ("157 具体版本", r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\157.0.4341.42"),
    ("156 具体版本", r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\156.0.4314.58"),
]
for name, folder in cases:
    ver = wintypes.LPWSTR()
    hr = dll.GetAvailableCoreWebView2BrowserVersionString(folder, ctypes.byref(ver))
    got = ver.value if hr == 0 else f"hr=0x{hr & 0xFFFFFFFF:08X}"
    print(f"  {name:24s} -> {got}", flush=True)

# 关键的另一个 API：GetAvailableCoreWebView2BrowserVersionStringWithOptions 不存在
# 改测 CompareBrowserVersions 确认 loader 工作正常
dll.CompareBrowserVersions.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
dll.CompareBrowserVersions.restype = ctypes.c_long
r = ctypes.c_int()
hr = dll.CompareBrowserVersions("157.0.4341.42", "130.0.0.0", ctypes.byref(r))
print(f"  CompareBrowserVersions -> hr=0x{hr & 0xFFFFFFFF:08X} r={r.value}", flush=True)

# 检查 loader 版本号（判断 loader 是否太旧）
PROD = ctypes.wintypes.DWORD()
dll.GetFileVersionInfoSizeW = ctypes.windll.version.GetFileVersionInfoSizeW
size = dll.GetFileVersionInfoSizeW(LOADER, None)
print("loader 文件大小:", os.path.getsize(LOADER), " 版本资源大小:", size, flush=True)
buf = ctypes.create_string_buffer(size)
ctypes.windll.version.GetFileVersionInfoW(LOADER, 0, size, buf)
p = ctypes.c_void_p(); n = ctypes.c_uint()
ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n))
class VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [("dwSignature", ctypes.c_ulong), ("dwStrucVersion", ctypes.c_ulong),
                ("dwFileVersionMS", ctypes.c_ulong), ("dwFileVersionLS", ctypes.c_ulong),
                ("dwProductVersionMS", ctypes.c_ulong), ("dwProductVersionLS", ctypes.c_ulong),
                ("dwFileFlagsMask", ctypes.c_ulong), ("dwFileFlags", ctypes.c_ulong),
                ("dwFileOS", ctypes.c_ulong), ("dwFileType", ctypes.c_ulong),
                ("dwFileSubtype", ctypes.c_ulong), ("dwFileDateMS", ctypes.c_ulong),
                ("dwFileDateLS", ctypes.c_ulong)]
fi = VS_FIXEDFILEINFO.from_address(p.value)
print(f"  WebView2Loader.dll 版本: {fi.dwFileVersionMS >> 16}.{fi.dwFileVersionMS & 0xFFFF}."
      f"{fi.dwFileVersionLS >> 16}.{fi.dwFileVersionLS & 0xFFFF}", flush=True)
