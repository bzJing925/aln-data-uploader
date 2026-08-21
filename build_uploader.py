#!/usr/bin/env python3
"""构建 aln-uploader 安装包（本机或 CI）。

macOS:   python build_uploader.py   → dist/ALN-Uploader.app + dist/aln-uploader-macos-arm64.zip
Windows: python build_uploader.py   → dist/aln-uploader-windows-amd64.zip
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

# Windows GBK 控制台打不了中文/特殊字符
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

# 明确排除主仓库 venv 里可能存在的重依赖（只按 import 图打包，不 collect app 全子模块）
EXCLUDES = [
    "torch", "pyarrow", "onnxruntime", "onnx", "sklearn", "matplotlib",
    "celery", "redis", "psycopg2", "sqlalchemy", "alembic", "PIL",
    "pytest", "IPython", "jupyter",
]


def main() -> None:
    dist = ROOT / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile" if platform.system() != "Darwin" else "--windowed",
        "--name", "ALN-Uploader",
        "--add-data", "src/app/webui/uploader/page.html:app/webui/uploader",
        "--collect-all", "skrf",
        "--hidden-import", "tkinter",
        "--hidden-import", "uvicorn.logging",
        "--clean", "--noconfirm",
    ]
    for ex in EXCLUDES:
        cmd += ["--exclude-module", ex]
    cmd.append("src/launch.py")
    print(">>", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)

    # 打包产物 → 规范命名的 zip
    if platform.system() == "Darwin":
        app = dist / "ALN-Uploader.app"
        if not app.exists():
            sys.exit("未找到 ALN-Uploader.app")
        out = dist / "aln-uploader-macos-arm64.zip"
        # ditto 保留符号链接（Python.framework 内部结构依赖 Current/ 链接；
        # shutil.make_archive 会把目录链接变成空目录 → 运行时 ModuleNotFoundError）
        subprocess.check_call(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(out)],
            cwd=ROOT,
        )
    else:
        exe = dist / "ALN-Uploader.exe"
        if not exe.exists():
            sys.exit("未找到 ALN-Uploader.exe")
        out = dist / "aln-uploader-windows-amd64.zip"
        shutil.make_archive(str(out.with_suffix("")), "zip", dist, "ALN-Uploader.exe")
    print("产物:", out)


if __name__ == "__main__":
    main()
