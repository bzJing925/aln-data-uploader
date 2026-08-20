"""ResonatorRow - 单个器件参数提取结果。

一条记录 = `devices` 表一行 = 一个谐振器在某个端口（S11 或 S22）的测量参数。
共 35 个字段（含元数据）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PortType = Literal["S11", "S22"]
PfFlag = Literal["Y", "N"]


class ResonatorRow(BaseModel):
    """一个器件 × 一个端口 的参数提取结果。"""

    # ── 文件信息 ─────────────────────────────────
    original_filename: str
    display_name: str
    folder_name: PortType  # 'S11' | 'S22'
    s_param_path: str = Field(description="S 参数文件相对路径（用于现读现画）")
    s_param_port: PortType = Field(default="S11", description="该记录对应的端口 S11/S22")

    # ── 几何 / 工艺 ──────────────────────────────
    wafer: int | None = None
    coord: str | None = None
    x: int | None = None
    y: int | None = None
    mark: str | None = Field(default=None, description="如 A1-1，可为 None")
    eg: float | None = None
    fl: float | None = None
    ag: float | None = None
    pf: PfFlag = "N"
    area_n: int | None = Field(default=None, description="mark 末尾数字（type 字段，不解释语义）")
    area_um2: int | None = Field(
        default=None, description="从 mapping description 解析（NNN&NNN 的对应面）"
    )

    # ── 主谐振参数 ──────────────────────────────
    fs_ghz: float
    fp_ghz: float
    zs_ohm: float
    zp_ohm: float

    # ── Q 系列 ─────────────────────────────────
    qs: float
    qp: float
    qs_bodeq: float
    qp_bodeq: float
    dbqs: float
    dbqp: float

    # ── BodeQ 系列 ──────────────────────────────
    bodeq_fitted: float
    bodeq_smooth: float
    bodeq_raw: float
    fbode_ghz: float

    # ── 耦合系数 ────────────────────────────────
    k2eff_pct: float

    # ── 中间寄生峰（可选） ───────────────────────
    fp2_ghz: float | None = None
    fs2_ghz: float | None = None
    zp2_ohm: float | None = None
    zs2_ohm: float | None = None

    # ── 处理标记 ────────────────────────────────
    deembedded: bool = False
