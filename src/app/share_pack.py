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

# ---------------------------------------------------------------------------
# 参数表格（xlsx）直传：表头别名 → devices 列
# ---------------------------------------------------------------------------

# 必填表头（用户提示文案里的最小集合）
XLSX_REQUIRED = [
    "original_filename", "display_name", "EG", "FL", "AG", "PF", "Area(um2)",
    "fs(GHz)", "fp(GHz)", "Zs(Ω)", "Zp(Ω)", "Qs", "Qp", "Qs_BodeQ", "Qp_BodeQ",
    "k2eff(%)",
]
# coord 与 X+Y 二选一
XLSX_HINT = (
    "表格的表头至少包含：original_filename、display_name、coord（或X和Y）、"
    "EG、FL、AG、PF、Area(um2)、fs(GHz)、fp(GHz)、Zs(Ω)、Zp(Ω)、Qs、Qp、"
    "Qs_BodeQ、Qp_BodeQ、k2eff(%)"
)

# 表头（归一化后）→ devices 列名；归一化 = 去空格/小写
_HEADER_ALIASES = {
    "original_filename": "original_filename",
    "display_name": "display_name",
    "folder_name": "folder_name",
    "mark": "mark",
    "wafer": "wafer",
    "coord": "coord",
    "x": "x",
    "y": "y",
    "eg": "eg",
    "fl": "fl",
    "ag": "ag",
    "pf": "pf",
    "area": "area_n",
    "area_n": "area_n",
    "area(um2)": "area_um2",
    "area_um2": "area_um2",
    "area(μm2)": "area_um2",
    "fs(ghz)": "fs_ghz",
    "fp(ghz)": "fp_ghz",
    "fs_ghz": "fs_ghz",
    "fp_ghz": "fp_ghz",
    "zs(ω)": "zs_ohm",
    "zp(ω)": "zp_ohm",
    "zs_ohm": "zs_ohm",
    "zp_ohm": "zp_ohm",
    "qs": "qs",
    "qp": "qp",
    "qs_bodeq": "qs_bodeq",
    "qp_bodeq": "qp_bodeq",
    "dbqs": "dbqs",
    "dbqp": "dbqp",
    "bodeq_fitted": "bodeq_fitted",
    "bodeq_smooth": "bodeq_smooth",
    "bodeq_raw": "bodeq_raw",
    "fbode(ghz)": "fbode_ghz",
    "fbode_ghz": "fbode_ghz",
    "k2eff(%)": "k2eff_pct",
    "k2eff_pct": "k2eff_pct",
    "kt2(%)": "k2eff_pct",
    "fp2(ghz)": "fp2_ghz",
    "fs2(ghz)": "fs2_ghz",
    "fp2_ghz": "fp2_ghz",
    "fs2_ghz": "fs2_ghz",
    "zs2(ω)": "zs2_ohm",
    "zp2(ω)": "zp2_ohm",
    "zs2_ohm": "zs2_ohm",
    "zp2_ohm": "zp2_ohm",
    "s_param_port": "s_param_port",
    "deembedded": "deembedded",
}

# 识别但平台不存储的列（提示里列出，不进包）
_HEADER_IGNORED = {
    "c0(pf)", "cm(pf)", "lm(nh)", "rm(ω)", "r0(ω)", "rs(ω)",
}


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", "", str(h)).lower()


def rows_from_xlsx(
    xlsx_path: str | Path,
    deembedded: bool,
    progress_cb: ProgressCb | None = None,
) -> tuple[list[dict], list[str], dict, dict, dict]:
    """参数表格 → (rows, failures, stats, mapping_entries, report)。

    deembedded 由调用方（页面勾选/CLI 参数）指定，写入每行与 meta。
    mapping_entries 从表格唯一 mark 派生（eg/fl/ag/area/pf 取该行值）。
    report 含 mapped/ignored/missing 列清单，供 UI 展示。
    """
    import pandas as pd

    cb = progress_cb or (lambda *_: None)
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise SystemExit(f"表格不存在: {xlsx_path}")
    cb("读表", 0, 1, f"读取 {xlsx_path.name}")
    df = pd.read_excel(xlsx_path)

    # 表头映射
    col_map: dict[str, str] = {}  # xlsx 列名 → devices 列名
    ignored: list[str] = []
    unknown: list[str] = []
    for c in df.columns:
        key = _norm_header(c)
        if key in _HEADER_ALIASES:
            col_map[c] = _HEADER_ALIASES[key]
        elif key in _HEADER_IGNORED:
            ignored.append(str(c))
        else:
            unknown.append(str(c))

    have = set(col_map.values())
    missing = [h for h in ("original_filename", "display_name", "eg", "fl", "ag", "pf",
                           "area_um2", "fs_ghz", "fp_ghz", "zs_ohm", "zp_ohm", "qs", "qp",
                           "qs_bodeq", "qp_bodeq", "k2eff_pct")
               if h not in have]
    if "coord" not in have and not ("x" in have and "y" in have):
        missing.append("coord（或X和Y）")
    if missing:
        raise SystemExit(f"表格缺少必填列: {missing}\n{XLSX_HINT}")

    cb("转换", 0, len(df), "逐行转换")
    pos = {src: df.columns.get_loc(src) for src in col_map}
    rows: list[dict] = []
    failures: list[str] = []
    for i, rec in enumerate(df.itertuples(index=False, name=None), 1):
        r = {dev: rec[pos[src]] for src, dev in col_map.items()}
        row = {c: None for c in PACK_COLUMNS}
        for dev, v in r.items():
            row[dev] = None if pd.isna(v) else v
        if isinstance(row.get("pf"), str):
            row["pf"] = row["pf"].strip().upper() or None
        # 派生字段
        fname = str(row.get("original_filename") or "")
        parsed = parse_filename(fname)
        if row.get("mark") is None:
            row["mark"] = parsed.mark
        if row.get("coord") is None:
            row["coord"] = parsed.coord
        if row.get("x") is None:
            row["x"] = parsed.x
        if row.get("y") is None:
            row["y"] = parsed.y
        folder = str(row.get("folder_name") or "")
        if row.get("s_param_port") is None:
            if folder.upper().startswith("S11"):
                row["s_param_port"] = "S11"
            elif folder.upper().startswith("S22"):
                row["s_param_port"] = "S22"
            elif parsed.port:
                row["s_param_port"] = parsed.port
        row["deembedded"] = bool(deembedded)
        if not fname:
            failures.append(f"第 {i} 行: original_filename 为空，已跳过")
            continue
        rows.append(row)
        if i % 5000 == 0 or i == len(df):
            cb("转换", i, len(df), f"{i}/{len(df)}")

    if not rows:
        raise SystemExit("表格无有效数据行")

    # 从唯一 mark 派生对照表条目
    by_mark: dict[str, dict] = {}
    for row in rows:
        m = row.get("mark") or "_"
        if m not in by_mark:
            by_mark[m] = {
                "mark": m if m != "_" else "",
                "description": row.get("display_name") or "",
                "eg": row.get("eg"),
                "fl": row.get("fl"),
                "ag": row.get("ag"),
                "area_s11": None,
                "area_s22": None,
                "has_pf": row.get("pf") == "Y",
            }
        e = by_mark[m]
        if row.get("s_param_port") == "S11" and e["area_s11"] is None:
            e["area_s11"] = row.get("area_um2")
        if row.get("s_param_port") == "S22" and e["area_s22"] is None:
            e["area_s22"] = row.get("area_um2")
        if e["has_pf"] is not True and row.get("pf") == "Y":
            e["has_pf"] = True
    mapping_entries = [by_mark[k] for k in sorted(by_mark)]

    ports = {r.get("s_param_port") for r in rows} - {None}
    if ports == {"S11", "S22"}:
        ptype = "BOTH"
    elif ports == {"S22"}:
        ptype = "S2P"
    else:
        ptype = "S1P"
    stats = {
        "process_type": ptype,
        "deembedded": bool(deembedded),
        "deembed_method": None,
        "source": "xlsx",
    }
    report = {
        "mapped": sorted(set(col_map.values())),
        "ignored_mbvd": ignored,
        "unknown": unknown,
        "n_rows": len(rows),
        "n_marks": len(mapping_entries),
    }
    return rows, failures, stats, mapping_entries, report


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


def make_share_pack_xlsx(
    xlsx_path: str | Path,
    batch_no: str,
    out_path: str | Path,
    deembedded: bool,
    mapping_name: str = "",
    f_start_ghz: float | None = None,
    f_end_ghz: float | None = None,
    progress_cb: ProgressCb | None = None,
) -> tuple[dict, dict]:
    """参数表格 → 分享包。返回 (meta, report)。

    mapping_name 为空时默认用对照表派生名（'auto_<batch_no>'）；
    与网站已有对照表同名时 CI 以现有为准。
    """
    rows, failures, stats, mapping_entries, report = rows_from_xlsx(
        xlsx_path, deembedded, progress_cb
    )
    wafer = _wafer_from_batch_no(batch_no)
    if wafer is not None:
        for row in rows:
            if row.get("wafer") is None:
                row["wafer"] = wafer
    name = mapping_name or f"auto_{batch_no.lstrip('#')}"
    meta = write_pack(
        rows,
        {
            "batch_no": batch_no,
            "mapping_name": name,
            "f_start_ghz": f_start_ghz,
            "f_end_ghz": f_end_ghz,
            **stats,
        },
        {"name": name, "entries": mapping_entries},
        failures,
        out_path,
    )
    size_mb = Path(out_path).stat().st_size / 1024 / 1024
    print(f"分享包已生成: {out_path}  ({size_mb:.1f} MB, {len(rows)} 行, 失败 {len(failures)})")
    return meta, report
