"""谐振参数提取（核心算法）。

来源：客户脚本 process_device_file + 多个辅助函数：
- find_resonances        (fs/fp 检测)
- detect_intermediate_peak (中间寄生峰)
- calculate_bodeq        (BodeQ 平滑/拟合)

注：客户脚本中的 mBVD 等效电路参数（C0/Cm/Lm/Rm/R0/Rs）已废弃，不实现。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from app.config import AlgorithmConfig, get_algorithm_config
from app.core.filename import parse_filename
from app.core.mapping import MappingEntry, get_area_for_port
from app.schemas.resonator import ResonatorRow

_AREA_N_RE = re.compile(r"-(\d+)$")


class ExtractError(Exception):
    """提取流程失败（文件无法读、谐振点异常、数据点不足等）。"""


# ── 1. 谐振点检测 ─────────────────────────────────────────────────────
def find_resonances(
    z_mag: np.ndarray,
    freq: np.ndarray,
    config: AlgorithmConfig | None = None,
) -> tuple[int, int]:
    """返回 (fs_idx, fp_idx)。

    fs：阻抗最低点（局部最小中最深的）。
    fp：fs 之后、间距 ≥ min_separation_hz 的所有局部最大里阻抗最高的。
    """
    cfg = config or get_algorithm_config()
    z_db = 20 * np.log10(np.maximum(z_mag, cfg.z_db_floor))

    minima: list[int] = []
    maxima: list[int] = []
    for i in range(1, len(z_db) - 1):
        if z_db[i] < z_db[i - 1] and z_db[i] < z_db[i + 1]:
            minima.append(i)
        elif z_db[i] > z_db[i - 1] and z_db[i] > z_db[i + 1]:
            maxima.append(i)

    if minima:
        fs_idx = int(minima[int(np.argmin(z_db[minima]))])
    else:
        fs_idx = int(np.argmin(z_db))

    candidate_peaks: list[int] = []
    for peak in maxima:
        if freq[peak] <= freq[fs_idx]:
            continue
        if (freq[peak] - freq[fs_idx]) < cfg.min_separation_hz:
            continue
        candidate_peaks.append(peak)

    if candidate_peaks:
        fp_idx = int(candidate_peaks[int(np.argmax(z_db[candidate_peaks]))])
    elif maxima:
        fp_idx = int(maxima[int(np.argmax(z_db[maxima]))])
    else:
        fp_idx = int(np.argmax(z_db))

    return fs_idx, fp_idx


# ── 2. BodeQ 平滑/拟合 ────────────────────────────────────────────────
def _bodeq_raw_array(s: np.ndarray, freq: np.ndarray) -> tuple[np.ndarray, int]:
    """计算原始 BodeQ 数组及有效点数。"""
    omega = 2 * np.pi * freq
    group_delay = -np.gradient(np.unwrap(np.angle(s))) / (2 * np.pi * np.gradient(freq))
    s_mag = np.abs(s)

    denominator = 1 - s_mag**2
    # 仅排除 |s| 极接近 1 导致除零的点；|s| = 0 时分子也为 0，BodeQ = 0 是合法值。
    valid_mask = denominator > 1e-6
    numerator = omega * group_delay * s_mag

    bodeq_raw = np.full_like(denominator, np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        bodeq_raw[valid_mask] = np.abs(numerator[valid_mask] / denominator[valid_mask])
    return bodeq_raw, int(np.count_nonzero(valid_mask))


def _interpolate_nan(arr: np.ndarray, freq: np.ndarray) -> np.ndarray:
    """对数组中的 NaN 用相邻有限值线性插值填充（边缘保持 NaN）。"""
    finite = np.isfinite(arr)
    if not finite.any():
        return arr
    filled = arr.copy()
    filled[~finite] = np.interp(freq[~finite], freq[finite], arr[finite])
    return filled


def _smooth_bodeq(bodeq_raw: np.ndarray, freq: np.ndarray, cfg: AlgorithmConfig) -> np.ndarray:
    """Savitzky-Golay 平滑 BodeQ 曲线（窗口随长度缩放，自动处理 NaN）。"""
    from scipy.signal import savgol_filter

    window_size = min(cfg.savgol_window, len(freq) // 10 * 2 + 1)
    if window_size <= 5:
        return bodeq_raw
    try:
        filled = _interpolate_nan(bodeq_raw, freq)
        return savgol_filter(filled, window_length=window_size, polyorder=cfg.savgol_polyorder)
    except Exception as exc:
        raise ExtractError(f"BodeQ Savitzky-Golay 平滑失败: {exc}") from exc


def _lorentzian(f: np.ndarray, amp: float, f0: float, gamma: float) -> np.ndarray:
    return amp * (gamma**2) / ((f - f0) ** 2 + gamma**2)


def calc_bodeq(
    s: np.ndarray,
    freq: np.ndarray,
    fs_idx: int,
    fp_idx: int,
    config: AlgorithmConfig | None = None,
) -> dict[str, float]:
    """对 S 参数做 BodeQ 计算 + 洛伦兹峰拟合。

    返回 bodeq_fitted/bodeq_smooth/bodeq_raw（在 Fbode 处的三种值）+
    fbode_ghz（拟合得到的中心频率）+
    qs_bodeq/qp_bodeq（fs/fp 索引处的平滑值，对应客户脚本里的 Qs_BodeQ/Qp_BodeQ）。
    """
    cfg = config or get_algorithm_config()

    bodeq_raw_arr, valid_count = _bodeq_raw_array(s, freq)
    if valid_count < 10:
        raise ExtractError(f"BodeQ 有效数据点不足（{valid_count} 点）")

    bodeq_smooth_arr = _smooth_bodeq(bodeq_raw_arr, freq, cfg)
    if np.all(np.isnan(bodeq_smooth_arr)):
        raise ExtractError("平滑后的 BodeQ 数组全为 NaN")

    max_idx = int(np.nanargmax(bodeq_smooth_arr))
    f_peak_guess = float(freq[max_idx])

    peak_range = int(len(freq) * cfg.lorentz_peak_range_ratio)
    start_idx = max(0, max_idx - peak_range)
    end_idx = min(len(freq), max_idx + peak_range)

    fit_freq = freq[start_idx:end_idx]
    fit_bodeq = bodeq_smooth_arr[start_idx:end_idx]

    amp0 = float(np.nanmax(fit_bodeq))
    f00 = f_peak_guess
    gamma0 = (float(freq[-1]) - float(freq[0])) / 100.0

    from scipy.interpolate import interp1d
    from scipy.optimize import curve_fit

    try:
        popt, _ = curve_fit(
            _lorentzian,
            fit_freq,
            fit_bodeq,
            p0=[amp0, f00, gamma0],
            bounds=(
                [0.1 * amp0, 0.9 * f00, gamma0 / 10],
                [10 * amp0, 1.1 * f00, 10 * gamma0],
            ),
        )
    except Exception as exc:
        raise ExtractError(f"BodeQ 洛伦兹拟合失败: {exc}") from exc

    amp, f0, gamma = popt
    bodeq_fitted = float(_lorentzian(np.asarray([f0]), amp, f0, gamma)[0])

    interp_smooth = interp1d(
        freq, bodeq_smooth_arr, kind="cubic", bounds_error=False, fill_value="extrapolate"
    )
    interp_raw = interp1d(
        freq, bodeq_raw_arr, kind="cubic", bounds_error=False, fill_value="extrapolate"
    )
    bodeq_smooth_at_peak = float(interp_smooth(f0))
    bodeq_raw_at_peak = float(interp_raw(f0))

    qs_bodeq = (
        float(bodeq_smooth_arr[fs_idx]) if 0 <= fs_idx < len(bodeq_smooth_arr) else float("nan")
    )
    qp_bodeq = (
        float(bodeq_smooth_arr[fp_idx]) if 0 <= fp_idx < len(bodeq_smooth_arr) else float("nan")
    )

    return {
        "bodeq_fitted": bodeq_fitted,
        "bodeq_smooth": bodeq_smooth_at_peak,
        "bodeq_raw": bodeq_raw_at_peak,
        "fbode_ghz": float(f0) / 1e9,
        "qs_bodeq": qs_bodeq,
        "qp_bodeq": qp_bodeq,
    }


def calc_bodeq_curve(
    s: np.ndarray,
    freq: np.ndarray,
    config: AlgorithmConfig | None = None,
) -> dict[str, list]:
    """返回完整的 BodeQ 曲线，用于现读现画。

    返回：
        {'freq_ghz': [...], 'raw': [...], 'smooth': [...], 'fitted': [...]}
        - raw: 原始 BodeQ 数组（NaN 填充无效点）
        - smooth: Savgol 平滑后的曲线
        - fitted: 在峰附近 ±peak_range 点上的洛伦兹拟合曲线，其他位置为 NaN
    """
    cfg = config or get_algorithm_config()

    bodeq_raw_arr, valid_count = _bodeq_raw_array(s, freq)
    if valid_count < 10:
        raise ExtractError(f"BodeQ 有效数据点不足（{valid_count} 点）")

    bodeq_smooth_arr = _smooth_bodeq(bodeq_raw_arr, freq, cfg)
    if np.all(np.isnan(bodeq_smooth_arr)):
        raise ExtractError("平滑后的 BodeQ 数组全为 NaN")

    fitted_arr = np.full_like(bodeq_smooth_arr, np.nan, dtype=float)

    from scipy.optimize import curve_fit

    try:
        max_idx = int(np.nanargmax(bodeq_smooth_arr))
        f_peak_guess = float(freq[max_idx])

        peak_range = int(len(freq) * cfg.lorentz_peak_range_ratio)
        start_idx = max(0, max_idx - peak_range)
        end_idx = min(len(freq), max_idx + peak_range)

        fit_freq = freq[start_idx:end_idx]
        fit_bodeq = bodeq_smooth_arr[start_idx:end_idx]

        amp0 = float(np.nanmax(fit_bodeq))
        f00 = f_peak_guess
        gamma0 = (float(freq[-1]) - float(freq[0])) / 100.0

        popt, _ = curve_fit(
            _lorentzian,
            fit_freq,
            fit_bodeq,
            p0=[amp0, f00, gamma0],
            bounds=(
                [0.1 * amp0, 0.9 * f00, gamma0 / 10],
                [10 * amp0, 1.1 * f00, 10 * gamma0],
            ),
        )
        amp, f0, gamma = popt
        fitted_arr[start_idx:end_idx] = _lorentzian(fit_freq, amp, f0, gamma)
    except Exception:
        # 拟合失败不致命，曲线接口仍返回 raw/smooth；fitted 全 NaN 即可
        pass

    def _to_list(arr: np.ndarray) -> list:
        return [None if (isinstance(v, float) and np.isnan(v)) else float(v) for v in arr]

    return {
        "freq_ghz": (freq / 1e9).tolist(),
        "raw": _to_list(bodeq_raw_arr),
        "smooth": _to_list(bodeq_smooth_arr),
        "fitted": _to_list(fitted_arr),
    }


# ── 3. 相位法 Q ───────────────────────────────────────────────────────
def calc_q_phase(z: np.ndarray, freq: np.ndarray, fs_idx: int, fp_idx: int) -> tuple[float, float]:
    """从阻抗相位斜率计算 Qs / Qp。"""
    z_phase = np.angle(z)
    phase_deriv = np.gradient(z_phase, freq)
    qs = float(abs(freq[fs_idx] * phase_deriv[fs_idx] / 2))
    qp = float(abs(freq[fp_idx] * phase_deriv[fp_idx] / 2))
    return qs, qp


# ── 4. 3dB 带宽法 Q ───────────────────────────────────────────────────
def calc_q_3db(
    z_db: np.ndarray,
    freq: np.ndarray,
    zs_db: float,
    zp_db: float,
    fs: float,
    fp: float,
) -> tuple[float, float]:
    """从 ±3dB 带宽估计 Qs / Qp，无法估计时返回 NaN。"""
    fs_idx = int(np.argmin(np.abs(freq - fs)))
    fp_idx = int(np.argmin(np.abs(freq - fp)))

    if fs_idx > 0:
        left_idx = int(np.argmin(np.abs(z_db[:fs_idx] - (zs_db + 3))))
    else:
        left_idx = 0
    right_idx = fs_idx + int(np.argmin(np.abs(z_db[fs_idx:] - (zs_db + 3))))
    dbqs = float(fs / (freq[right_idx] - freq[left_idx])) if right_idx > left_idx else float("nan")

    if fp_idx > 0:
        left_idx = fp_idx - int(np.argmin(np.abs(z_db[:fp_idx][::-1] - (zp_db - 3))))
    else:
        left_idx = 0
    right_idx = fp_idx + int(np.argmin(np.abs(z_db[fp_idx:] - (zp_db - 3))))
    dbqp = float(fp / (freq[right_idx] - freq[left_idx])) if right_idx > left_idx else float("nan")

    return dbqs, dbqp


# ── 5. 中间寄生峰检测 ─────────────────────────────────────────────────
def detect_intermediate_peak(
    freq: np.ndarray,
    z_mag_db: np.ndarray,
    fs_idx: int,
    fp_idx: int,
    zs: float,
    zp: float,
    config: AlgorithmConfig | None = None,
) -> dict[str, float] | None:
    """在 fs..fp 区间内识别寄生峰对（fp2 局部最大、fs2 紧随其后的局部最小）。

    物理约束：fs < fp2 < fs2 < fp 且 zs < zs2 < zp2 < zp。无候选返回 None。
    """
    cfg = config or get_algorithm_config()

    from scipy.signal import find_peaks, savgol_filter

    freq = np.asarray(freq)
    z_mag_db = np.asarray(z_mag_db)

    if fs_idx >= fp_idx:
        return None

    region = slice(fs_idx, fp_idx + 1)
    z_region = z_mag_db[region]
    f_region = freq[region]
    n_region = len(z_region)

    if n_region < 20:
        return None

    window = int(n_region * cfg.intermediate_peak_smooth_window_ratio)
    window = max(5, window)
    if window % 2 == 0:
        window += 1
    window = min(window, 201)
    polyorder = 3 if window > 7 else 2

    try:
        z_smooth = savgol_filter(z_region, window_length=window, polyorder=polyorder)
    except Exception:
        return None

    distance = max(3, window // 2)
    peaks_local, peak_props = find_peaks(
        z_smooth, distance=distance, prominence=cfg.intermediate_peak_prominence_db
    )
    valleys_local, valley_props = find_peaks(
        -z_smooth, distance=distance, prominence=cfg.intermediate_peak_prominence_db
    )

    if len(peaks_local) == 0 or len(valleys_local) == 0:
        return None

    zs_db_val = float(z_mag_db[fs_idx])
    zp_db_val = float(z_mag_db[fp_idx])
    min_sep = (freq[fp_idx] - freq[fs_idx]) * cfg.intermediate_peak_min_valley_sep_ratio

    candidates: list[dict[str, float]] = []
    for p_idx in peaks_local:
        fp2_freq = float(f_region[p_idx])
        zp2_db = float(z_region[p_idx])

        if fp2_freq <= freq[fs_idx] or fp2_freq >= freq[fp_idx]:
            continue

        subsequent_valleys = [v for v in valleys_local if v > p_idx]
        if not subsequent_valleys:
            continue

        v_idx = subsequent_valleys[0]
        fs2_freq = float(f_region[v_idx])
        zs2_db = float(z_region[v_idx])

        if not (freq[fs_idx] < fp2_freq < fs2_freq < freq[fp_idx]):
            continue
        if not (zs_db_val < zs2_db < zp2_db < zp_db_val):
            continue
        if (fs2_freq - fp2_freq) < min_sep:
            continue

        score = zp2_db - zs2_db
        p_prom = float(peak_props["prominences"][np.where(peaks_local == p_idx)[0][0]])
        v_prom = float(valley_props["prominences"][np.where(valleys_local == v_idx)[0][0]])

        candidates.append(
            {
                "fp2_freq": fp2_freq,
                "fs2_freq": fs2_freq,
                "zp2_db": zp2_db,
                "zs2_db": zs2_db,
                "score": score,
                "peak_prominence": p_prom,
                "valley_prominence": v_prom,
            }
        )

    if not candidates:
        return None

    # 抑制未使用变量告警 (zs/zp 用于 future 增强约束；当前以 dB 复用 zs_db/zp_db)
    _ = (zs, zp)

    best = max(candidates, key=lambda x: x["score"])
    return {
        "fp2_ghz": best["fp2_freq"] / 1e9,
        "fs2_ghz": best["fs2_freq"] / 1e9,
        "zp2_ohm": float(10 ** (best["zp2_db"] / 20)),
        "zs2_ohm": float(10 ** (best["zs2_db"] / 20)),
    }


# ── 6. 主流程：单文件 → ResonatorRow ─────────────────────────────────
def _parse_area_n(mark: str | None) -> int | None:
    """从 mark 提取末尾数字（'A1-3' → 3）。"""
    if not mark:
        return None
    m = _AREA_N_RE.search(mark)
    return int(m.group(1)) if m else None


def extract_resonator_params(
    s1p_path: str | Path,
    *,
    mapping: dict[str, MappingEntry] | None = None,
    wafer: int | None = None,
    s_param_relpath: str = "",
    deembedded: bool = False,
    f_start_ghz: float | None = None,
    f_end_ghz: float | None = None,
    config: AlgorithmConfig | None = None,
    skip_validation: bool = False,
    port: int = 0,
) -> ResonatorRow | dict[str, Any]:
    """对单个 .s1p 文件提取 24 列谐振参数。

    Args:
        skip_validation: 为 True 时直接返回 dict，跳过 Pydantic ResonatorRow
            校验。Worker 批量处理场景可节省 10–20% 时间。
    """
    cfg = config or get_algorithm_config()
    s1p_path = Path(s1p_path)
    filename = s1p_path.name

    import skrf as rf

    parsed = parse_filename(filename)

    try:
        dut = rf.Network(str(s1p_path))
    except Exception as exc:
        raise ExtractError(f"加载 S1P 失败 {filename}: {exc}") from exc

    # 频率范围筛选
    if f_start_ghz is not None or f_end_ghz is not None:
        original_freq = dut.f
        start_idx = 0
        end_idx = len(original_freq) - 1
        if f_start_ghz is not None:
            start_idx = int(np.argmin(np.abs(original_freq - f_start_ghz * 1e9)))
        if f_end_ghz is not None:
            end_idx = int(np.argmin(np.abs(original_freq - f_end_ghz * 1e9)))
        if start_idx >= end_idx:
            raise ExtractError(f"{filename} 在指定频率范围内无有效数据")
        dut = dut[start_idx : end_idx + 1]

    freq = dut.f
    n_points = len(freq)
    if n_points < 10:
        raise ExtractError(f"{filename} 数据点不足（{n_points} 点）")

    s = dut.s[:, 0, 0] if dut.nports == 1 else dut.s[:, port, port]
    z = dut.z[:, 0, 0] if dut.nports == 1 else dut.z[:, port, port]
    z_mag = np.abs(z)

    fs_idx, fp_idx = find_resonances(z_mag, freq, cfg)
    fs = float(freq[fs_idx])
    fp = float(freq[fp_idx])

    if fs >= fp:
        raise ExtractError(f"{filename} 谐振点异常 (fs={fs / 1e9:.3f}GHz, fp={fp / 1e9:.3f}GHz)")

    zs = float(z_mag[fs_idx])
    zp = float(z_mag[fp_idx])

    qs, qp = calc_q_phase(z, freq, fs_idx, fp_idx)

    z_db = 20 * np.log10(np.maximum(z_mag, cfg.z_db_floor))
    zs_db = float(z_db[fs_idx])
    zp_db = float(z_db[fp_idx])
    dbqs, dbqp = calc_q_3db(z_db, freq, zs_db, zp_db, fs, fp)

    bodeq = calc_bodeq(s, freq, fs_idx, fp_idx, cfg)

    z_mag_db = 20 * np.log10(np.maximum(z_mag, cfg.z_db_floor))
    intermediate = detect_intermediate_peak(freq, z_mag_db, fs_idx, fp_idx, zs, zp, cfg)

    k2eff_pct = float((np.pi**2 / 4) * (fs / fp) * ((fp - fs) / fp) * 100)

    # 端口语义：s1p 文件名里已含 S11/S22；s2p 按传入 port 参数决定
    port_name = parsed.port if parsed.port else ("S11" if port == 0 else "S22")

    # mapping 解析
    mark = parsed.mark
    entry = mapping.get(mark) if (mapping and mark) else None
    eg = entry.eg if entry else None
    fl = entry.fl if entry else None
    ag = entry.ag if entry else None
    pf = "Y" if (entry and entry.has_pf) else parsed.pf
    area_um2 = get_area_for_port(entry, port_name) if entry else None
    display_name = entry.description if entry else filename
    folder_name = port_name
    area_n = _parse_area_n(mark)

    payload: dict[str, Any] = {
        "original_filename": filename,
        "display_name": display_name,
        "folder_name": folder_name,
        "s_param_path": s_param_relpath,
        "s_param_port": port_name,
        "wafer": wafer,
        "coord": parsed.coord,
        "x": parsed.x,
        "y": parsed.y,
        "mark": mark,
        "eg": eg,
        "fl": fl,
        "ag": ag,
        "pf": pf,
        "area_n": area_n,
        "area_um2": area_um2,
        "fs_ghz": fs / 1e9,
        "fp_ghz": fp / 1e9,
        "zs_ohm": zs,
        "zp_ohm": zp,
        "qs": qs,
        "qp": qp,
        "qs_bodeq": bodeq["qs_bodeq"],
        "qp_bodeq": bodeq["qp_bodeq"],
        "dbqs": dbqs,
        "dbqp": dbqp,
        "bodeq_fitted": bodeq["bodeq_fitted"],
        "bodeq_smooth": bodeq["bodeq_smooth"],
        "bodeq_raw": bodeq["bodeq_raw"],
        "fbode_ghz": bodeq["fbode_ghz"],
        "k2eff_pct": k2eff_pct,
        "fp2_ghz": intermediate["fp2_ghz"] if intermediate else None,
        "fs2_ghz": intermediate["fs2_ghz"] if intermediate else None,
        "zp2_ohm": intermediate["zp2_ohm"] if intermediate else None,
        "zs2_ohm": intermediate["zs2_ohm"] if intermediate else None,
        "deembedded": deembedded,
    }
    if skip_validation:
        return payload
    return ResonatorRow(**payload)
