# -*- coding: utf-8 -*-
"""
面试猴 · 版本发布脚本（release.py）

功能：
1. 校验版本号格式（语义化版本 x.y.z）
2. 更新 package.json 中的 version 字段
3. 快照当前源码到 versions/v<版本号>/（排除 dist、node_modules、缓存等）
4. 在 CHANGELOG.md 顶部插入本次版本记录
5. 在 versions/RELEASES.md 追加一行流水账

用法：
    python release.py --version 1.0.1 --note "修复 Ollama 引擎 404 问题"
    python release.py --version 1.1.0 --note "新增系统音频采集" --changes "新增:xxx;修复:yyy"

不加参数运行 `python release.py --show` 可查看当前版本和已有版本列表。
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PACKAGE_JSON = APP_DIR / "package.json"
CHANGELOG = APP_DIR / "CHANGELOG.md"
VERSIONS_DIR = APP_DIR / "versions"
RELEASES_MD = VERSIONS_DIR / "RELEASES.md"

# 快照时排除的目录/文件（构建产物、依赖、缓存、虚拟环境）
IGNORE_PATTERNS = shutil.ignore_patterns(
    "node_modules*", "dist*", "build*", "__pycache__*", "*.pyc",
    "venv*", ".venv*", "*.exe", "versions", ".git*", "release.py",
    "diag_sr*.py", "test_*.py",  # 临时诊断脚本和测试脚本不入正式快照
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_current_version():
    with open(PACKAGE_JSON, "r", encoding="utf-8") as f:
        return json.load(f).get("version", "0.0.0")


def list_released_versions():
    if not VERSIONS_DIR.exists():
        return []
    return sorted(
        p.name for p in VERSIONS_DIR.iterdir()
        if p.is_dir() and p.name.startswith("v")
    )


def show():
    print(f"当前版本: v{read_current_version()}")
    released = list_released_versions()
    if released:
        print("已发快照版本:")
        for v in released:
            print(f"  - {v}")
    else:
        print("（还没有任何版本快照，运行 --version x.y.z 开始第一次发版）")


def snapshot(new_ver: str) -> Path:
    """把当前源码快照到 versions/v<ver>/"""
    dest = VERSIONS_DIR / f"v{new_ver}"
    if dest.exists():
        sys.exit(f"[错误] 版本快照 {dest} 已存在，请换一个版本号。")
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        APP_DIR, dest,
        ignore=IGNORE_PATTERNS,
        dirs_exist_ok=False,
    )
    return dest


def update_package_json(new_ver: str):
    with open(PACKAGE_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    old = data.get("version")
    data["version"] = new_ver
    with open(PACKAGE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[OK] package.json: {old} -> {new_ver}")


def insert_changelog(new_ver: str, note: str, changes: str):
    today = datetime.now().strftime("%Y-%m-%d")
    entry_lines = [f"## [{new_ver}] - {today}"]
    if changes:
        # 支持 "新增:xxx;修复:yyy" 格式，按类型分条
        for item in changes.split(";"):
            item = item.strip()
            if not item or ":" not in item:
                continue
            tag, desc = item.split(":", 1)
            entry_lines.append(f"- **{tag.strip()}** {desc.strip()}")
    elif note:
        entry_lines.append(f"- {note}")

    with open(CHANGELOG, "r", encoding="utf-8") as f:
        content = f.read()

    marker = "---\n\n## [1.0.0]"
    insert_at = content.find(marker)
    if insert_at == -1:
        sys.exit("[错误] CHANGELOG.md 中找不到插入锚点（## [1.0.0] 之前），请手动检查。")

    entry = "\n".join(entry_lines) + "\n\n---\n\n"
    content = content[:insert_at] + entry + content[insert_at:]

    with open(CHANGELOG, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] CHANGELOG.md 已插入 [{new_ver}] 记录")


def append_release_log(new_ver: str, note: str):
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_new = not RELEASES_MD.exists()
    with open(RELEASES_MD, "a", encoding="utf-8") as f:
        if is_new:
            f.write("# 面试猴 · 版本流水账\n\n| 版本 | 发布时间 | 说明 |\n|---|---|---|\n")
        f.write(f"| v{new_ver} | {today} | {note} |\n")
    print(f"[OK] versions/RELEASES.md 已追加 v{new_ver}")


def main():
    ap = argparse.ArgumentParser(description="面试猴版本发布工具")
    ap.add_argument("--version", help="新版本号，如 1.0.1")
    ap.add_argument("--note", default="", help="一句话版本说明")
    ap.add_argument("--changes", default="", help="分条变更，格式: 新增:xxx;修复:yyy")
    ap.add_argument("--snapshot-only", action="store_true",
                    help="只做源码快照，不改 package.json、不写 CHANGELOG（用于打基线）")
    ap.add_argument("--show", action="store_true", help="只查看当前版本，不发布")
    args = ap.parse_args()

    if args.show or not args.version:
        show()
        return

    ver = args.version.lstrip("v")
    if not SEMVER_RE.match(ver):
        sys.exit(f"[错误] 版本号格式不合法: {args.version}（应为 x.y.z，如 1.0.1）")

    old_ver = read_current_version()
    note = args.note or f"从 v{old_ver} 升级"

    print(f"发布 v{old_ver} -> v{ver}，说明: {note}")
    dest = snapshot(ver)
    print(f"[OK] 源码快照: {dest}")
    if args.snapshot_only:
        append_release_log(ver, note)
        print(f"\n✅ 基线快照 v{ver} 完成（package.json 与 CHANGELOG 未改动）。")
        return
    update_package_json(ver)
    insert_changelog(ver, note, args.changes)
    append_release_log(ver, note)
    print(f"\n✅ 版本 v{ver} 发布完成。回滚任意版本：直接运行 versions/v{ver}/ 下的代码即可。")


if __name__ == "__main__":
    main()
