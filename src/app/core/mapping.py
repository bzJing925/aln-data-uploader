"""对照表加载 + Description token 解析。

对照表格式（xlsx，无表头）：
| Mark   | Description           |
| A1-1   | EG0 FL0 700&5500      |
| A2-3   | EG0 FL0.5 1200&4500   |
| C1-1   | EG1 FL0 AG0.5 800&5200 |
| D1-1   | EG0+PF FL0 900&5000   |

解析规则：
- EG/FL/AG：浮点数，缺失为 None
- area_s11 / area_s22：从 'NNN&NNN' 拆解，第一个数 → S11 面积，第二个 → S22 面积
- has_pf：description 中含 '+PF' 或独立 'PF' token → True
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_EG_RE = re.compile(r"\bEG([\d.]+)")
_FL_RE = re.compile(r"\bFL([\d.]+)")
_AG_RE = re.compile(r"\bAG([\d.]+)")
_PF_RE = re.compile(r"(?:\+PF|\bPF\b)")
_AREA_PAIR_RE = re.compile(r"\b(\d+)&(\d+)\b")


@dataclass(frozen=True)
class MappingEntry:
    mark: str
    description: str
    eg: float | None = None
    fl: float | None = None
    ag: float | None = None
    area_s11: int | None = None
    area_s22: int | None = None
    has_pf: bool = False
    raw_tokens: tuple[str, ...] = field(default_factory=tuple)


def load_mapping(path: str | Path) -> dict[str, MappingEntry]:
    """从 xlsx/csv 加载对照表，返回 {mark: MappingEntry}。"""
    import pandas as pd

    if isinstance(path, bytes):
        path = path.decode("utf-8")
    path = Path(path)
    if not path.exists():
        return {}
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, header=None, dtype=str)
    else:
        df = pd.read_excel(path, header=None, dtype=str)

    out: dict[str, MappingEntry] = {}
    for _, row in df.iterrows():
        if len(row) < 2 or pd.isna(row.iloc[0]) or pd.isna(row.iloc[1]):
            continue
        mark = str(row.iloc[0]).strip()
        desc = str(row.iloc[1]).strip()
        if not mark or not desc:
            continue
        out[mark] = parse_description(mark, desc)
    return out


def parse_description(mark: str, description: str) -> MappingEntry:
    """把 'EG0 FL0 700&5500' 解析成结构化字段。"""
    eg = _parse_float(_EG_RE.search(description))
    fl = _parse_float(_FL_RE.search(description))
    ag = _parse_float(_AG_RE.search(description))
    has_pf = bool(_PF_RE.search(description))

    area_s11 = area_s22 = None
    am = _AREA_PAIR_RE.search(description)
    if am:
        area_s11, area_s22 = int(am.group(1)), int(am.group(2))

    return MappingEntry(
        mark=mark,
        description=description,
        eg=eg,
        fl=fl,
        ag=ag,
        area_s11=area_s11,
        area_s22=area_s22,
        has_pf=has_pf,
        raw_tokens=tuple(description.split()),
    )


def _parse_float(m: re.Match | None) -> float | None:
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


def get_area_for_port(entry: MappingEntry, port: str) -> int | None:
    """根据端口（'S11'/'S22'）取对应面积。"""
    return entry.area_s11 if port.upper() == "S11" else entry.area_s22
