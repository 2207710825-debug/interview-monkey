# -*- mode: python ; coding: utf-8 -*-
"""面试猴 Windows 单文件包。

包含流式 Transducer 与第二遍 SenseVoice 模型，保证仅本地模式可用。
"""
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all


project_dir = Path(SPECPATH)

datas = [
    (str(project_dir / "renderer"), "renderer"),
    (str(project_dir / "config"), "config"),
    (str(project_dir / "prompts"), "prompts"),
    (str(project_dir / "web"), "web"),
    (str(project_dir / "fixtures" / "realtime_mock.json"), "fixtures"),
    (str(project_dir / "engine" / "asr_models"), "engine/asr_models"),
    (str(project_dir / "engine" / "asr_models_zh_en"), "engine/asr_models_zh_en"),
    (str(project_dir / "engine" / "asr_models_sense"), "engine/asr_models_sense"),
    (str(project_dir / "engine" / "knowledge"), "engine/knowledge"),
    (str(project_dir / "engine" / "corrections.json"), "engine"),
    (str(project_dir / "engine" / "hotwords.txt"), "engine"),
    (str(project_dir / "engine" / "terms.txt"), "engine"),
]
binaries = []

# 当前 Python 来自 Miniconda，_ctypes/_sqlite3 的运行库位于 Library/bin。
# PyInstaller 不会自动把这个目录加入 DLL 搜索路径，因此显式随包携带。
conda_bin = Path(sys.base_prefix) / "Library" / "bin"
for dll_name in ("ffi.dll", "sqlite3.dll"):
    dll_path = conda_bin / dll_name
    if not dll_path.is_file():
        raise FileNotFoundError(f"打包依赖缺失: {dll_path}")
    binaries.append((str(dll_path), "."))

hiddenimports = [
    "scipy.signal",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "webview.platforms.win32",
]

# 这些包包含动态导入、模型运行库或 JSON 词典，静态扫描无法完整发现。
for package in ("webview", "sherpa_onnx", "pyaudiowpatch", "pypinyin", "uvicorn"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

analysis = Analysis(
    [str(project_dir / "面试猴.py")],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "cefpython3",
        "gi",
        # scipy 的兼容层会在安装了这些开发依赖时探测它们；程序运行并不需要。
        "torch",
        "torchaudio",
        "torchvision",
        "pytest",
        "py",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="面试猴",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
