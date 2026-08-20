"""aln-uploader 启动入口（PyInstaller 打包入口）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.webui.uploader.server import main

main()
