"""S2P → S1P 拆分，以及 .snp 通用扩展名自动识别。

来源：客户脚本 split_header_data / modify_header / extract_s11_data / extract_s22_data /
        write_output_file / process_s2p_file（27-1196 行去重后版本）。

S2P 文件每行：freq + S11_re + S11_im + S21_re + S21_im + S12_re + S12_im + S22_re + S22_im
- S11 拆分：保留前 3 列（freq, S11_re, S11_im）
- S22 拆分：保留第 1 列和最后 2 列（freq, S22_re, S22_im）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SplitResult:
    s11_path: Path
    s22_path: Path


def detect_snp_type(path: str | Path) -> str:
    """通过首行数据列数判断 .snp 文件是 S1P 还是 S2P。

    - 3 列 → S1P（freq + S11_re + S11_im）
    - 9 列 → S2P（freq + 4 组 S 参数）
    其他列数抛异常。
    """
    path = Path(path)
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("!") or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) == 3:
                return "S1P"
            if len(parts) == 9:
                return "S2P"
            raise ValueError(
                f"无法识别 {path.name} 的端口类型："
                f"数据行列数为 {len(parts)}，期望 3（S1P）或 9（S2P）"
            )
    raise ValueError(f"{path.name} 没有有效数据行")


def split_s2p_to_s1p(
    s2p_path: str | Path,
    out_dir_s11: str | Path,
    out_dir_s22: str | Path,
    *,
    lowercase: bool = False,
) -> SplitResult:
    """把一个 .s2p 文件拆成 S11 和 S22 两个 .s1p 文件。

    返回两个输出文件路径。会创建 out_dir_s11 / out_dir_s22 目录。
    lowercase=True 时输出后缀为 `_s11.s1p` / `_s22.s1p`，否则为 `_S11.s1p` / `_S22.s1p`。
    """
    s2p_path = Path(s2p_path)
    out_dir_s11 = Path(out_dir_s11)
    out_dir_s22 = Path(out_dir_s22)
    out_dir_s11.mkdir(parents=True, exist_ok=True)
    out_dir_s22.mkdir(parents=True, exist_ok=True)

    stem = s2p_path.stem
    sfx_s11, sfx_s22 = ("_s11", "_s22") if lowercase else ("_S11", "_S22")
    s11_path = out_dir_s11 / f"{stem}{sfx_s11}.s1p"
    s22_path = out_dir_s22 / f"{stem}{sfx_s22}.s1p"

    with open(s2p_path) as f:
        lines = f.readlines()

    header, data = _split_header_data(lines)
    s11_header = _modify_header(header, "S11")
    s22_header = _modify_header(header, "S22")
    s11_data = _extract_s11(data)
    s22_data = _extract_s22(data)

    _write(s11_path, s11_header, s11_data)
    _write(s22_path, s22_header, s22_data)

    return SplitResult(s11_path=s11_path, s22_path=s22_path)


def _split_header_data(content: list[str]) -> tuple[list[str], list[str]]:
    header: list[str] = []
    data: list[str] = []
    header_ended = False
    for line in content:
        stripped = line.strip()
        if not header_ended:
            if stripped.startswith("!") or stripped.startswith("#") or not stripped:
                header.append(line)
            else:
                data.append(line)
                header_ended = True
        else:
            data.append(line)
    return header, data


def _modify_header(header: list[str], parameter: str) -> list[str]:
    new_header: list[str] = []
    if len(header) >= 2:
        new_header.extend(header[:2])

    for line in header:
        if line.strip().startswith(f"!Correction: {parameter}("):
            new_header.append(line)
            break

    for line in header:
        if line.strip().startswith(("!S2P File:", "!S1P File:")):
            new_line = line.replace("S2P", "S1P").replace("S11, S21, S12, S22", parameter)
            new_header.append(new_line)
            break

    option_line = unit_line = None
    for line in header:
        stripped = line.strip()
        if stripped.startswith("#"):
            option_line = line
        elif stripped.lower().startswith("hz"):
            unit_line = line
    if option_line:
        new_header.append(option_line)
    if unit_line:
        new_header.append(unit_line)
    return new_header


def _extract_s11(data_lines: list[str]) -> list[str]:
    # S2P 数据行格式：freq + S11_re/im + S21_re/im + S12_re/im + S22_re/im = 9 列。
    # 列数 < 9 视为损坏行 → raise，避免与 _extract_s22 产生数量不对齐的 S11/S22
    # 进而让下游 skrf de-embed 拿到对不上 frequency 轴的两个文件、静默腐败结果。
    out: list[str] = []
    for lineno, line in enumerate(data_lines, start=1):
        parts = line.strip().split()
        if not parts:
            continue  # 空行（常见的末尾换行）跳过
        if len(parts) < 9:
            raise ValueError(f"S2P 数据行 {lineno} 列数 {len(parts)} < 9，文件可能截断或损坏")
        out.append(f"{parts[0]} {parts[1]} {parts[2]}\n")
    return out


def _extract_s22(data_lines: list[str]) -> list[str]:
    out: list[str] = []
    for lineno, line in enumerate(data_lines, start=1):
        parts = line.strip().split()
        if not parts:
            continue
        if len(parts) < 9:
            raise ValueError(f"S2P 数据行 {lineno} 列数 {len(parts)} < 9，文件可能截断或损坏")
        out.append(f"{parts[0]} {parts[-2]} {parts[-1]}\n")
    return out


def _write(path: Path, header: list[str], data: list[str]) -> None:
    with open(path, "w") as f:
        f.writelines(header)
        f.writelines(data)
