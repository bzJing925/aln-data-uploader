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
        shutil.make_archive(str(out.with_suffix("")), "zip", dist, "ALN-Uploader.app")
    else:
        exe = dist / "ALN-Uploader.exe"
        if not exe.exists():
            sys.exit("未找到 ALN-Uploader.exe")
        out = dist / "aln-uploader-windows-amd64.zip"
        shutil.make_archive(str(out.with_suffix("")), "zip", dist, "ALN-Uploader.exe")
    print("产物:", out)


if __name__ == "__main__":
    main()
