"""简易上传页面：分享包制作 + 可选一键提交（双击即用的本地小工具）。

功能对齐软件版上传页：多文件(zip/s1p/s2p/snp)、对照表（网站已有/本地 xlsx）、
频率范围、去嵌开关+方法、进度条；产出 MB 级分享包，可直接提交 GitHub 触发网站更新。

开发模式：cd backend && uv run python -m app.webui.uploader
打包后：双击运行（--browse 子命令用于原生文件选择框，勿手动调）
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.core.mapping import load_mapping
from app.share_pack import (
    mapping_entries_from_json,
    mapping_entries_to_json,
)

SITE = "https://bzjing925.github.io/aln-data-web"
REPO = "bzJing925/aln-data-web"
CONFIG_PATH = Path.home() / ".aln-uploader" / "config.json"

# 打包进度全局状态（单用户本地工具，无需会话隔离）
_state: dict = {
    "running": False, "stage": "", "current": 0, "total": 0, "msg": "",
    "done": False, "error": None, "pack_path": None, "meta": None,
}
_state_lock = threading.Lock()


def _set_state(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def _progress(stage: str, cur: int, total: int, msg: str) -> None:
    _set_state(stage=stage, current=cur, total=total, msg=msg)


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


def _browse_native(kind: str) -> str:
    """弹原生文件选择框（子进程保证 tkinter 在主线程；打包后 exe 自调用）。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--browse", kind]
    else:
        cmd = [sys.executable, "-m", "app.webui.uploader", "--browse", kind]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return r.stdout.strip()


def browse_dialog(kind: str) -> None:
    """--browse 子命令入口：弹窗打印所选路径。"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if kind == "dir":
        path = filedialog.askdirectory(title="选择输出目录")
    elif kind == "mapping":
        path = filedialog.askopenfilename(
            title="选择对照表", filetypes=[("对照表", "*.xlsx *.xls *.csv")]
        )
    else:
        path = filedialog.askopenfilename(
            title="选择数据文件",
            filetypes=[("数据文件", "*.zip *.s1p *.s2p *.snp"), ("所有文件", "*.*")],
        )
    print(path or "")
    root.destroy()


class PackReq(BaseModel):
    inputs: list[str]
    batch_no: str
    mapping_mode: str  # "site" | "local"
    mapping_name: str = ""
    mapping_path: str = ""
    f_start: float | None = None
    f_end: float | None = None
    deembed: bool = False
    deembed_method: str = "default"
    out_dir: str = ""


class SubmitReq(BaseModel):
    pack_path: str
    token: str = ""
    remember: bool = False


def _ssl_context():
    """打包环境（python.org Python）不读系统钥匙串 → 显式用 certifi 根证书。"""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_json(url: str, token: str = "", method: str = "GET", payload: dict | None = None,
               timeout: int = 20):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "aln-uploader")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=timeout, context=_ssl_context()) as resp:
        return json.loads(resp.read())


def _fetch_site() -> dict:
    """拉网站批次/对照表（离线降级）。"""
    out = {"online": False, "batches": [], "mappings": []}
    try:
        b = _http_json(f"{SITE}/webdata/batches.json", timeout=8)
        m = _http_json(f"{SITE}/webdata/mappings.json", timeout=8)
        out = {
            "online": True,
            "batches": [x["batch_no"] for x in b],
            "mappings": [
                {"name": x["name"], "entry_count": x.get("entry_count", len(x.get("entries", []))),
                 "entries": x.get("entries", [])}
                for x in m
            ],
        }
    except Exception:
        pass
    return out


def _run_pack(req: PackReq) -> None:
    try:
        _set_state(running=True, done=False, error=None, pack_path=None, meta=None)
        if req.mapping_mode == "site":
            site = _fetch_site()
            m = next((x for x in site["mappings"] if x["name"] == req.mapping_name), None)
            if not m:
                raise SystemExit(f"网站上找不到对照表 {req.mapping_name}（离线时可改用本地 xlsx）")
            mapping = mapping_entries_from_json(m["name"], m["entries"])
            mapping_json = mapping_entries_to_json(m["name"], mapping)
            mapping_name = m["name"]
        else:
            mp = Path(req.mapping_path)
            mapping = load_mapping(mp)
            if not mapping:
                raise SystemExit(f"对照表为空或解析失败: {mp}")
            mapping_json = mapping_entries_to_json(mp.stem, mapping)
            mapping_name = mp.stem

        out_dir = Path(req.out_dir or str(Path.home() / "Desktop"))
        safe = req.batch_no.replace("#", "").replace("/", "_").replace(" ", "_")
        out = out_dir / f"分享包_{safe}.zip"

        from app.share_pack import run_extraction, write_pack

        rows, failures, stats = run_extraction(
            [Path(p) for p in req.inputs], mapping, req.batch_no,
            deembed=req.deembed, deembed_method=req.deembed_method,
            progress_cb=_progress,
        )
        _progress("写包", 0, 1, "写出分享包")
        meta = write_pack(
            rows,
            {
                "batch_no": req.batch_no,
                "mapping_name": mapping_name,
                "f_start_ghz": req.f_start,
                "f_end_ghz": req.f_end,
                **stats,
            },
            mapping_json,
            failures,
            out,
        )
        _set_state(running=False, done=True, pack_path=str(out), meta=meta,
                   msg=f"完成：{len(rows)} 行")
    except Exception as e:
        _set_state(running=False, done=True, error=str(e), msg="失败")


def _sanitize(name: str) -> str:
    keep = []
    for ch in name:
        keep.append(ch if (ch.isalnum() or ch in "._-") else "_")
    return "".join(keep)


def _submit(pack_path: str, token: str) -> str:
    """上传分享包到新分支并开 PR，返回 PR 链接。"""
    pack = Path(pack_path)
    content = base64.b64encode(pack.read_bytes()).decode()
    api = f"https://api.github.com/repos/{REPO}"
    ref = _http_json(f"{api}/git/ref/heads/main", token)
    base_sha = ref["object"]["sha"]
    import time

    branch = f"upload/{_sanitize(pack.stem)}-{int(time.time())}"
    _http_json(f"{api}/git/refs", token, "POST",
               {"ref": f"refs/heads/{branch}", "sha": base_sha})
    _http_json(f"{api}/contents/uploads/{_sanitize(pack.name)}", token, "PUT", {
        "message": f"upload: 分享包 {pack.name}",
        "content": content,
        "branch": branch,
    })
    pr = _http_json(f"{api}/pulls", token, "POST", {
        "title": f"分享包：{pack.stem}",
        "head": branch,
        "base": "main",
        "body": "CI 校验通过后合并即自动更新网站。",
    })
    return pr["html_url"]


def create_app() -> FastAPI:
    app = FastAPI(title="aln-uploader")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.post("/api/browse")
    def browse(body: dict):
        return {"path": _browse_native(body.get("kind", "zip"))}

    @app.get("/api/site")
    def site():
        return _fetch_site()

    @app.post("/api/pack")
    def pack(req: PackReq):
        if _state.get("running"):
            return JSONResponse({"error": "已有任务在运行"}, status_code=409)
        threading.Thread(target=_run_pack, args=(req,), daemon=True).start()
        return {"started": True}

    @app.get("/api/progress")
    def progress():
        with _state_lock:
            return dict(_state)

    @app.post("/api/submit")
    def submit(req: SubmitReq):
        token = req.token.strip() or _load_config().get("token", "")
        if not token:
            return JSONResponse({"error": "未提供 GitHub token"}, status_code=400)
        try:
            url = _submit(req.pack_path, token)
        except urllib.error.HTTPError as e:
            return JSONResponse({"error": f"GitHub API {e.code}: {e.read()[:200]!r}"},
                                status_code=502)
        if req.remember:
            _save_config({"token": token})
        return {"pr_url": url}

    @app.get("/api/config")
    def config():
        return {"has_token": bool(_load_config().get("token")), "site": SITE, "repo": REPO}

    @app.post("/api/open_dir")
    def open_dir(body: dict):
        p = Path(body.get("path", "")).parent
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True}

    @app.post("/api/shutdown")
    def shutdown():
        # 打包成 .app 后没有控制台窗口，提供显式退出
        def _bye():
            import os
            import time

            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_bye, daemon=True).start()
        return {"ok": True}

    return app


def main() -> None:
    if "--browse" in sys.argv:
        browse_dialog(sys.argv[sys.argv.index("--browse") + 1])
        return
    import uvicorn

    port = 8630
    app = create_app()
    import webbrowser

    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"简易上传页面已启动: http://127.0.0.1:{port}  （关闭本窗口即退出）")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


_PAGE_FILE = Path(__file__).resolve().parent / "page.html"
_PAGE = _PAGE_FILE.read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
