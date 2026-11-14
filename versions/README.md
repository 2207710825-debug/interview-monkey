# versions/ · 版本快照目录

每个 `vX.Y.Z/` 子目录是该版本发布时的**完整源码快照**，可直接运行，也可随时回滚。

## 结构

```
versions/
├─ RELEASES.md        # 版本流水账（时间 + 说明一览表）
├─ v1.0.0/            # 基线快照：首个可运行版本
└─ ...
```

## 快照包含 / 不包含

- ✅ 包含：`src/`、`renderer/`、`engine/`、`package.json`、`README.md`、`CHANGELOG.md`、正式 Python 脚本
- ❌ 不包含：`dist/`、`node_modules/`、`*.exe`、`__pycache__`、临时诊断脚本（`diag_sr*.py`、`test_*.py`）

## 常用操作

```bash
# 查看当前版本和已有快照
python release.py --show

# 发布新版本（快照 + 更新 package.json + 写 CHANGELOG）
python release.py --version 1.0.1 --note "修复 xxx"
# 或分条记录：
python release.py --version 1.1.0 --note "新增系统音频采集" --changes "新增:系统音频回环采集;修复:转写中断问题"

# 只打快照（不打正式版本号，如中间里程碑）
python release.py --version 1.0.0-rc1 --note "里程碑" --snapshot-only
```

## 回滚方法

直接把某个版本目录复制回去即可：

```bash
# 例如回滚到 v1.0.0
cp -r versions/v1.0.0/src ./
cp versions/v1.0.0/package.json ./
```
