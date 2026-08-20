"""分享包核心逻辑（CLI scripts/make_share_pack.py 与简易上传页面共用）。

格式契约：format_version=1；devices.csv.gz（PACK_COLUMNS）+ meta.json + mapping.json。
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.core.filename import parse_filename
from app.core.mapping import MappingEntry, load_mapping
from app.workers.pipeline.calibration import CalibrationIndex
from app.workers.pipeline.processor import DutProcessor

FORMAT_VERSION = 1

# 分享包列（= devices 表减去 id/batch_id/s_param_path；merge 侧再赋 id/batch_no）
PACK_COLUMNS = [
    "original_filename", "display_name", "mark", "wafer", "folder_name", "coord",
    "x", "y", "eg", "fl", "ag", "pf", "area_n", "area_um2",
    "fs_ghz", "fp_ghz", "zs_ohm", "zp_ohm", "qs", "qp", "qs_bodeq", "qp_bodeq",
    "dbqs", "dbqp", "bodeq_fitted", "bodeq_smooth", "bodeq_raw", "fbode_ghz",
    "k2eff_pct", "fp2_ghz", "fs2_ghz", "zp2_ohm", "zs2_ohm", "deembedded", "s_param_port",
]

_CAL_KEYWORDS = ("OPEN", "SHORT", "WO", "WS")

ProgressCb = Callable[[str, int, int, str], None]  # (stage, current, total, msg)


def _looks_like_calibration(name: str) -> bool:
    upper = name.upper()
    return any(kw in upper for kw in _CAL_KEYWORDS)


def _wafer_from_batch_no(batch_no: str) -> int | None:
    m = re.search(r"\.(\d+)$", batch_no)
    return int(m.group(1)) if m else None


def mapping_entries_to_json(name: str, mapping: dict[str, MappingEntry]) -> dict:
    """已解析对照表 → 分享包 mapping.json 结构。"""
    return {
        "name": name,
        "entries": [
            {
                "mark": e.mark,
                "description": e.description,
                "eg": e.eg,
                "fl": e.fl,
                "ag": e.ag,
                "area_s11": e.area_s11,
                "area_s22": e.area_s22,
                "has_pf": e.has_pf,
            }
            for e in sorted(mapping.values(), key=lambda x: x.mark)
        ],
    }


def mapping_entries_from_json(name: str, entries: list[dict]) -> dict[str, MappingEntry]:
    """网站 mappings.json 的 entries → MappingEntry 字典（无需 xlsx 也能提取）。"""
    out = {}
    for e in entries:
        out[e["mark"]] = MappingEntry(
            mark=e["mark"],
            description=e.get("description") or "",
            eg=e.get("eg"),
            fl=e.get("fl"),
            ag=e.get("ag"),
            area_s11=e.get("area_s11"),
            area_s22=e.get("area_s22"),
            has_pf=bool(e.get("has_pf")),
        )
    return out


def run_extraction(
    inputs: list[Path],
    mapping: dict[str, MappingEntry],
    batch_no: str,
    deembed: bool = False,
    deembed_method: str = "default",
    progress_cb: ProgressCb | None = None,
) -> tuple[list[dict], list[str], dict]:
    """解压/收集输入 → 逐 DUT 提取。返回 (rows, failures, stats)。

    inputs 可混合 zip 与 .s1p/.s2p 散文件（散文件按单批次直接处理）。
    deembed=True 时 zip 内需含 OPEN/SHORT 校准 .s2p（与平台一致）。
    """
    cb = progress_cb or (lambda *_: None)
    wafer = _wafer_from_batch_no(batch_no)
    processor = DutProcessor(compress_raw=False, keep_deembed_temp=False)

    tmp = Path(tempfile.mkdtemp(prefix=f"sharepack_{batch_no.strip('#').replace('/', '_')}_"))
    rows: list[dict] = []
    failures: list[str] = []
    try:
        # ── 收集文件（zip 解压 + 散文件拷贝）──
        cb("解压", 0, len(inputs), "准备输入文件")
        for i, src in enumerate(inputs, 1):
            src = Path(src)
            if not src.exists():
                raise SystemExit(f"输入不存在: {src}")
            cb("解压", i - 1, len(inputs), f"处理 {src.name}")
            if src.suffix.lower() == ".zip":
                with zipfile.ZipFile(src) as zf:
                    zf.extractall(tmp / src.stem)
            elif src.suffix.lower() in (".s1p", ".s2p", ".snp"):
                shutil.copy(src, tmp / src.name)
            else:
                raise SystemExit(f"不支持的文件类型: {src.name}（仅 .zip/.s1p/.s2p/.snp）")
        cb("解压", len(inputs), len(inputs), "解压完成")

        # ── 扫描分类（与 pipeline_batch_task 一致：校准件单独建索引）──
        cal_s2p: list[Path] = []
        items: list[dict] = []
        n_types = {"s1p": 0, "s2p": 0}
        for p in sorted(tmp.rglob("*")):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in (".s1p", ".s2p"):
                continue
            if parse_filename(p.name).is_calibration or _looks_like_calibration(p.name):
                if ext == ".s2p":
                    cal_s2p.append(p)
                continue
            item_type = "s2p" if ext == ".s2p" else "s1p"
            n_types[item_type] += 1
            items.append(
                {"type": item_type, "path": str(p), "s_param_relpath": str(p.relative_to(tmp))}
            )
        if not items:
            raise SystemExit("输入内未发现可处理的 .s1p/.s2p 器件文件")

        cal_index = None
        if deembed:
            if not cal_s2p:
                raise SystemExit("开启了去嵌但未发现校准件（文件名含 OPEN/SHORT 的 .s2p）")
            cb("校准", 0, 1, f"建立校准索引（{len(cal_s2p)} 个校准件，方法 {deembed_method}）")
            cal_index = CalibrationIndex.build(tmp, cal_s2p, method=deembed_method)

        # ── 逐 DUT 提取 ──
        total = len(items)
        for i, item in enumerate(items, 1):
            result = processor.process(item, mapping, wafer, cal_index, tmp)
            rows.extend(result["rows"])
            failures.extend(result["failures"])
            cb("提取", i, total, f"{i}/{total}（失败 {len(failures)}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not rows:
        raise SystemExit("全部器件提取失败，未生成分享包")

    stats = {
        "process_type": (
            "BOTH" if n_types["s1p"] and n_types["s2p"] else ("S2P" if n_types["s2p"] else "S1P")
        ),
        "deembedded": deembed,
        "deembed_method": deembed_method if deembed else None,
    }
    return rows, failures, stats


def write_pack(
    rows: list[dict],
    meta_extra: dict,
    mapping_json: dict,
    failures: list[str],
    out_path: str | Path,
) -> dict:
    """rows + meta → 分享包 zip。返回完整 meta。"""
    clean_rows = []
    for r in rows:
        r = dict(r)
        r.pop("s_param_path", None)  # 网站不展示原文件路径
        clean_rows.append(r)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    text_buf = io.StringIO()
    writer = csv.DictWriter(text_buf, fieldnames=PACK_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in clean_rows:
        writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in PACK_COLUMNS})
    csv_bytes = gzip.compress(text_buf.getvalue().encode("utf-8"), compresslevel=6, mtime=0)

    meta = {
        "format_version": FORMAT_VERSION,
        "device_count": len(clean_rows),
        "failure_count": len(failures),
        "created_at": datetime.now(UTC).isoformat(),
        "generator": f"make_share_pack.py@{FORMAT_VERSION}",
        "sha256_devices_csv_gz": hashlib.sha256(csv_bytes).hexdigest(),
        **meta_extra,
    }

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        zout.writestr("mapping.json", json.dumps(mapping_json, ensure_ascii=False))
        zout.writestr("devices.csv.gz", csv_bytes)
        if failures:
            zout.writestr("failures.txt", "\n".join(failures))
    return meta


def make_share_pack(
    inputs: str | Path | list[str | Path],
    batch_no: str,
    mapping_path: str | Path,
    out_path: str | Path,
    f_start_ghz: float | None = None,
    f_end_ghz: float | None = None,
    deembed: bool = False,
    deembed_method: str = "default",
    progress_cb: ProgressCb | None = None,
) -> dict:
    """输入（zip/散文件列表）→ 分享包。返回 meta dict。CLI/简易页面共用入口。"""
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    mapping_path = Path(mapping_path)
    if not mapping_path.exists():
        raise SystemExit(f"对照表不存在: {mapping_path}")
    mapping = load_mapping(mapping_path)
    if not mapping:
        raise SystemExit(f"对照表为空或解析失败: {mapping_path}")

    rows, failures, stats = run_extraction(
        [Path(p) for p in inputs], mapping, batch_no,
        deembed=deembed, deembed_method=deembed_method, progress_cb=progress_cb,
    )
    meta = write_pack(
        rows,
        {
            "batch_no": batch_no,
            "mapping_name": mapping_path.stem,
            "f_start_ghz": f_start_ghz,
            "f_end_ghz": f_end_ghz,
            **stats,
        },
        mapping_entries_to_json(mapping_path.stem, mapping),
        failures,
        out_path,
    )
    size_mb = Path(out_path).stat().st_size / 1024 / 1024
    print(f"分享包已生成: {out_path}  ({size_mb:.1f} MB, {len(rows)} 行, 失败 {len(failures)})")
    return meta
