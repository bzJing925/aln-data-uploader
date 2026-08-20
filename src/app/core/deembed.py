"""ShortOpen de-embedding 封装，支持多版本匹配策略。

来源：客户提供的 4 个去嵌脚本（de.py / de_GSG100_ELB003.py /
de_ELB003_VZ.py / de_ELB003_Basic.py）。

设计：
- 去嵌操作本身（ShortOpen.deembed）对所有方法一致。
- 区别在于 DUT 与 OPEN/SHORT 校准件的匹配策略。
- 当前平台上传的是 ZIP（含 s2p），校准件会被 split_s2p_to_s1p
  拆成 S11/S22 两个 s1p；拆分后的文件名含 `_S11` / `_S22` 后缀。
  匹配前先 strip 端口后缀，恢复原始 stem，再按各脚本逻辑解析。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path


class DeembedMethod(StrEnum):
    """去嵌方法。"""

    DEFAULT = "default"  # 平台默认：同目录第一组校准件
    ORIGINAL = "original"  # de.py：按 (sxx, suffix_num, position) 匹配
    GSG100 = "gsg100"  # de_GSG100_ELB003.py：按 (prefix, key, rest) 精确匹配
    VZ = "vz"  # de_ELB003_VZ.py：支持 V-Z 器件特殊格式
    BASIC = "basic"  # de_ELB003_Basic.py：WO/WS 识别


class DeembedError(Exception):
    """去嵌失败。"""


# ─────────────────────────── 工具函数 ───────────────────────────

_PORT_SUFFIX_RE = re.compile(r"_(S\d{2})\.s1p$", re.IGNORECASE)


def _strip_port_suffix(name: str) -> str:
    """去掉 `_S11.s1p` / `_S22.s1p` 后缀，恢复原始文件名 stem。

    例：``OPEN_1_S11.s1p`` → ``OPEN_1``
    """
    return _PORT_SUFFIX_RE.sub("", name)


def _normalize_num(num_str: str) -> str:
    return str(int(num_str))


# ─────────────────────────── 解析函数 ───────────────────────────


def _parse_original(name: str) -> tuple[str | None, str, str]:
    """de.py 的 parse_filename：返回 (sxx, suffix_num, position)。

    支持拆分后的文件名，如 ``2_A1-1_X0Y0N20_Fail_S11.s1p``。
    """
    upper = name.upper()

    # 1. 提取 Sxx（拆分后的 _S11.s1p 或旧前缀 S11_...）
    sxx = None
    name_body = name
    m = re.search(r"_(S\d{2})\.S1P$", upper)
    if m:
        sxx = m.group(1)
        name_body = name[: m.start()]
    else:
        m = re.search(r"_(S\d{2})$", upper)
        if m:
            sxx = f"S{m.group(1)}"
            name_body = name[: m.start()]
        else:
            m = re.match(r"^(S\d{2})", upper)
            if m:
                sxx = m.group(1)

    if sxx is None:
        return None, "", "GLOBAL"

    # 2. 提取编号（连字符后的数字）
    suffix_num = None
    m = re.search(r"-(\d+)(?:_|$)", name_body)
    if m:
        suffix_num = _normalize_num(m.group(1))
    else:
        nums = re.findall(r"\d+", name_body)
        if nums:
            nums = [n for n in nums if n != sxx[1:]]
            suffix_num = _normalize_num(nums[-1]) if nums else "3"
        else:
            suffix_num = "3"

    # 3. 提取位置（包含 X/Y/N 的连续段）
    position = "GLOBAL"
    matches = list(re.finditer(r"([A-Z0-9\-]*[XYN][A-Z0-9\-]{2,})", name_body, re.IGNORECASE))
    if matches:
        position = matches[-1].group(1).upper()
    else:
        m2 = re.search(r"([A-Z0-9\-]{3,})(?=_|\.)", name_body, re.IGNORECASE)
        if m2:
            position = m2.group(1).upper()

    return sxx, suffix_num, position


def _parse_gsg100(name: str) -> tuple[str | None, str | None, str | None]:
    """de_GSG100_ELB003.py 的 parse_filename：返回 (prefix, key, rest)。"""
    parts = name.split("_", 2)
    if len(parts) < 3:
        return None, None, None
    prefix, second, rest = parts

    second_upper = second.upper()
    if second_upper.startswith("OPEN-") or second_upper.startswith("SHORT-"):
        if "-" in second:
            key = second.split("-", 1)[1][0].upper()
        else:
            key = None
        return prefix, key, rest
    else:
        if second:
            key = second[0].upper()
        else:
            key = None
        return prefix, key, rest


def _parse_vz(name: str) -> tuple[str | None, str, str, str | None]:
    """de_ELB003_VZ.py 的 parse_filename：返回 (sxx, suffix_num, position, device_letter)。

    在 _parse_original 基础上增加 device_letter 提取。
    """
    sxx, suffix_num, position = _parse_original(name)
    if sxx is None:
        return None, "", "GLOBAL", None

    name_body = name
    m = re.search(r"_(S\d{2})$", name.upper())
    if m:
        name_body = name[: m.start()]

    device_letter = None
    # V-Z 校准文件格式：VO-*、VS-*（字母后紧跟 O/S）
    m = re.search(r"(^|[^A-Z0-9])([V-Z])[SO][\-\d]", name_body, re.IGNORECASE)
    if m:
        device_letter = m.group(2).upper()
    else:
        # A-J 器件
        m = re.search(r"(^|[^A-Z0-9])([A-J])\d", name_body, re.IGNORECASE)
        if m:
            device_letter = m.group(2).upper()
        else:
            # V-Z DUT 格式（不含 S/O 后缀）
            m = re.search(r"(^|[^A-Z0-9])([V-Z])\d", name_body, re.IGNORECASE)
            if m:
                device_letter = m.group(2).upper()

    return sxx, suffix_num, position, device_letter


def _parse_basic(name: str) -> tuple[str | None, str, str]:
    """de_ELB003_Basic.py 的 parse_filename：与 de.py 相同。"""
    return _parse_original(name)


# ─────────────────────────── 校准件识别 ───────────────────────────

_OPEN_RE = re.compile(r"(?<![A-Za-z])OPEN(?![A-Za-z])", re.IGNORECASE)
_SHORT_RE = re.compile(r"(?<![A-Za-z])SHORT(?![A-Za-z])", re.IGNORECASE)


def _is_calibration_original(name: str) -> tuple[bool, bool]:
    """de.py 的校准文件识别：含 OPEN/SHORT 关键字。"""
    return bool(_OPEN_RE.search(name)), bool(_SHORT_RE.search(name))


def _is_calibration_gsg100(name: str) -> tuple[bool, bool]:
    """de_GSG100_ELB003.py 的校准文件识别：文件名第二段为 OPEN-* 或 SHORT-*。"""
    parts = name.split("_", 2)
    if len(parts) < 2:
        return False, False
    second = parts[1].upper()
    if second.startswith("OPEN-"):
        return True, False
    if second.startswith("SHORT-"):
        return False, True
    return False, False


def _is_calibration_vz(name: str) -> tuple[bool, bool]:
    """de_ELB003_VZ.py 的校准文件识别。

    V-Z 器件：VO-* / VS-* 格式。
    A-J 器件：含 OPEN/SHORT 关键字。
    """
    parsed = _parse_vz(name)
    device_letter = parsed[3]

    # V-Z 校准件
    if device_letter and device_letter in "VWXYZ":
        name_body = _strip_port_suffix(name)
        if re.search(rf"{device_letter}O[\-\d]", name_body, re.IGNORECASE):
            return True, False
        if re.search(rf"{device_letter}S[\-\d]", name_body, re.IGNORECASE):
            return False, True

    # A-J 校准件（fallback 到原始逻辑）
    return _is_calibration_original(name)


def _is_calibration_basic(name: str) -> tuple[bool, bool]:
    """de_ELB003_Basic.py 的校准文件识别：WO/WS 模式（不含 W1）。"""
    is_open = re.search(r"WO", name, re.IGNORECASE) and not re.search(r"W1", name, re.IGNORECASE)
    is_short = re.search(r"WS", name, re.IGNORECASE) and not re.search(r"W1", name, re.IGNORECASE)
    return is_open, is_short


# ─────────────────────────── 匹配核心 ───────────────────────────


def _match_original(
    dut_name: str,
    cal_files: list[str],
) -> tuple[str | None, str | None]:
    """de.py 匹配逻辑。返回 (open_name, short_name)。"""
    dut_sxx, dut_num, dut_pos = _parse_original(dut_name)
    if dut_sxx is None:
        return None, None

    # 建立索引
    position_index: dict[str, dict[str, dict[tuple[str, str], str]]] = {}
    for f in cal_files:
        is_open, is_short = _is_calibration_original(f)
        if not (is_open or is_short):
            continue
        cal_type = "OPEN" if is_open else "SHORT"
        sxx, cal_num, pos = _parse_original(f)
        if sxx is None:
            continue
        key = (cal_num, sxx)
        position_index.setdefault(pos, {}).setdefault(cal_type, {})[key] = f

    open_file = short_file = None
    key = (dut_num, dut_sxx)

    # 同 position 匹配
    if dut_pos in position_index:
        pos_files = position_index[dut_pos]
        if "OPEN" in pos_files and key in pos_files["OPEN"]:
            open_file = pos_files["OPEN"][key]
        elif "OPEN" in pos_files and ("3", dut_sxx) in pos_files["OPEN"]:
            open_file = pos_files["OPEN"][("3", dut_sxx)]
        if "SHORT" in pos_files and key in pos_files["SHORT"]:
            short_file = pos_files["SHORT"][key]
        elif "SHORT" in pos_files and ("3", dut_sxx) in pos_files["SHORT"]:
            short_file = pos_files["SHORT"][("3", dut_sxx)]

    # GLOBAL 兜底
    if (not open_file or not short_file) and "GLOBAL" in position_index:
        pos_files = position_index["GLOBAL"]
        if not open_file and "OPEN" in pos_files:
            if key in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][key]
            elif ("3", dut_sxx) in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][("3", dut_sxx)]
        if not short_file and "SHORT" in pos_files:
            if key in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][key]
            elif ("3", dut_sxx) in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][("3", dut_sxx)]

    return open_file, short_file


def _match_gsg100(
    dut_name: str,
    cal_files: list[str],
) -> tuple[str | None, str | None]:
    """de_GSG100_ELB003.py 匹配逻辑。返回 (open_name, short_name)。"""
    dut_prefix, dut_key, dut_rest = _parse_gsg100(dut_name)
    if dut_prefix is None:
        return None, None

    cal_dict: dict[tuple[str, str | None, str], dict[str, str]] = {}
    for f in cal_files:
        is_open, is_short = _is_calibration_gsg100(f)
        if not (is_open or is_short):
            continue
        prefix, key, rest = _parse_gsg100(f)
        if prefix is None:
            continue
        cal_type = "OPEN" if is_open else "SHORT"
        cal_dict.setdefault((prefix, key, rest), {})[cal_type] = f

    key = (dut_prefix, dut_key, dut_rest)
    cal = cal_dict.get(key)
    if not cal:
        return None, None

    return cal.get("OPEN"), cal.get("SHORT")


def _match_vz(
    dut_name: str,
    cal_files: list[str],
) -> tuple[str | None, str | None]:
    """de_ELB003_VZ.py 匹配逻辑。返回 (open_name, short_name)。"""
    dut_sxx, dut_num, dut_pos, dut_letter = _parse_vz(dut_name)
    if dut_sxx is None:
        return None, None

    # 建立索引
    position_index: dict[str, dict[str, dict[tuple[str, str], str]]] = {}
    vz_cal_index: dict[str, dict[str, dict[tuple[str, str, str], str]]] = {}

    for f in cal_files:
        parsed = _parse_vz(f)
        sxx, cal_num, pos, letter = parsed
        if sxx is None:
            continue

        # V-Z 校准件
        if letter and letter in "VWXYZ":
            cal_type = None
            name_body = _strip_port_suffix(f)
            if re.search(rf"{letter}O[\-\d]", name_body, re.IGNORECASE):
                cal_type = "OPEN"
            elif re.search(rf"{letter}S[\-\d]", name_body, re.IGNORECASE):
                cal_type = "SHORT"
            if cal_type:
                key = (letter, cal_num, sxx)
                vz_cal_index.setdefault(pos, {}).setdefault(cal_type, {})[key] = f
                continue

        # A-J 校准件
        is_open, is_short = _is_calibration_original(f)
        if not (is_open or is_short):
            continue
        cal_type = "OPEN" if is_open else "SHORT"
        key = (cal_num, sxx)
        position_index.setdefault(pos, {}).setdefault(cal_type, {})[key] = f

    open_file = short_file = None

    # V-Z 器件
    if dut_letter and dut_letter in "VWXYZ":
        cal_letter = "W" if dut_letter == "Z" else dut_letter
        key = (cal_letter, dut_num, dut_sxx)

        # 同 position
        if dut_pos in vz_cal_index:
            pos_files = vz_cal_index[dut_pos]
            if "OPEN" in pos_files and key in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][key]
            elif "OPEN" in pos_files and (cal_letter, "3", dut_sxx) in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][(cal_letter, "3", dut_sxx)]
            if "SHORT" in pos_files and key in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][key]
            elif "SHORT" in pos_files and (cal_letter, "3", dut_sxx) in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][(cal_letter, "3", dut_sxx)]

        # GLOBAL 兜底
        if (not open_file or not short_file) and "GLOBAL" in vz_cal_index:
            pos_files = vz_cal_index["GLOBAL"]
            if not open_file and "OPEN" in pos_files:
                if key in pos_files["OPEN"]:
                    open_file = pos_files["OPEN"][key]
                elif (cal_letter, "3", dut_sxx) in pos_files["OPEN"]:
                    open_file = pos_files["OPEN"][(cal_letter, "3", dut_sxx)]
            if not short_file and "SHORT" in pos_files:
                if key in pos_files["SHORT"]:
                    short_file = pos_files["SHORT"][key]
                elif (cal_letter, "3", dut_sxx) in pos_files["SHORT"]:
                    short_file = pos_files["SHORT"][(cal_letter, "3", dut_sxx)]

    # A-J 器件（与 original 相同）
    else:
        key = (dut_num, dut_sxx)
        if dut_pos in position_index:
            pos_files = position_index[dut_pos]
            if "OPEN" in pos_files and key in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][key]
            elif "OPEN" in pos_files and ("3", dut_sxx) in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][("3", dut_sxx)]
            if "SHORT" in pos_files and key in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][key]
            elif "SHORT" in pos_files and ("3", dut_sxx) in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][("3", dut_sxx)]

        if (not open_file or not short_file) and "GLOBAL" in position_index:
            pos_files = position_index["GLOBAL"]
            if not open_file and "OPEN" in pos_files:
                if key in pos_files["OPEN"]:
                    open_file = pos_files["OPEN"][key]
                elif ("3", dut_sxx) in pos_files["OPEN"]:
                    open_file = pos_files["OPEN"][("3", dut_sxx)]
            if not short_file and "SHORT" in pos_files:
                if key in pos_files["SHORT"]:
                    short_file = pos_files["SHORT"][key]
                elif ("3", dut_sxx) in pos_files["SHORT"]:
                    short_file = pos_files["SHORT"][("3", dut_sxx)]

    return open_file, short_file


def _match_basic(
    dut_name: str,
    cal_files: list[str],
) -> tuple[str | None, str | None]:
    """de_ELB003_Basic.py 匹配逻辑。返回 (open_name, short_name)。"""
    dut_sxx, dut_num, dut_pos = _parse_basic(dut_name)
    if dut_sxx is None:
        return None, None

    # 建立索引
    position_index: dict[str, dict[str, dict[tuple[str, str], str]]] = {}

    for f in cal_files:
        is_open, is_short = _is_calibration_basic(f)
        if not (is_open or is_short):
            continue
        cal_type = "OPEN" if is_open else "SHORT"
        sxx, cal_num, pos = _parse_basic(f)
        if sxx is None:
            continue
        key = (cal_num, sxx)
        position_index.setdefault(pos, {}).setdefault(cal_type, {})[key] = f

    open_file = short_file = None
    key = (dut_num, dut_sxx)

    # 同 position 匹配
    if dut_pos in position_index:
        pos_files = position_index[dut_pos]
        if "OPEN" in pos_files and key in pos_files["OPEN"]:
            open_file = pos_files["OPEN"][key]
        elif "OPEN" in pos_files and ("3", dut_sxx) in pos_files["OPEN"]:
            open_file = pos_files["OPEN"][("3", dut_sxx)]
        if "SHORT" in pos_files and key in pos_files["SHORT"]:
            short_file = pos_files["SHORT"][key]
        elif "SHORT" in pos_files and ("3", dut_sxx) in pos_files["SHORT"]:
            short_file = pos_files["SHORT"][("3", dut_sxx)]

    # GLOBAL 兜底
    if (not open_file or not short_file) and "GLOBAL" in position_index:
        pos_files = position_index["GLOBAL"]
        if not open_file and "OPEN" in pos_files:
            if key in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][key]
            elif ("3", dut_sxx) in pos_files["OPEN"]:
                open_file = pos_files["OPEN"][("3", dut_sxx)]
        if not short_file and "SHORT" in pos_files:
            if key in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][key]
            elif ("3", dut_sxx) in pos_files["SHORT"]:
                short_file = pos_files["SHORT"][("3", dut_sxx)]

    return open_file, short_file


# ─────────────────────────── 对外接口 ───────────────────────────


def match_calibration(
    dut_path: Path,
    cal_paths: list[Path],
    method: DeembedMethod = DeembedMethod.DEFAULT,
) -> tuple[Path, Path]:
    """为 DUT 匹配对应的 OPEN / SHORT 校准件。

    参数
    ----
    dut_path : DUT 的 s1p 文件路径。
    cal_paths : 该端口所有校准件 s1p 路径（已拆分后的，含 OPEN 与 SHORT）。
    method : 去嵌方法。

    返回
    ----
    (open_path, short_path)

    找不到则 raise DeembedError。
    """
    if method == DeembedMethod.DEFAULT:
        # 平台默认：取该端口下第一组可用校准件
        open_paths = [p for p in cal_paths if _is_calibration_original(p.name)[0]]
        short_paths = [p for p in cal_paths if _is_calibration_original(p.name)[1]]
        op = open_paths[0] if open_paths else None
        sh = short_paths[0] if short_paths else None
        if not op or not sh:
            raise DeembedError(f"DEFAULT 方法无法为 {dut_path.name} 找到校准件")
        return op, sh

    # 匹配时应保留端口后缀，否则拆分后的 DUT 名如 2_A1-1_X-1Y-1N27_Fail_S11.s1p
    # 去掉 _S11 后无法提取 Sxx，导致所有文件都解析为 GLOBAL 且 sxx=None，匹配失败。
    dut_name = dut_path.name
    cal_names = [p.name for p in cal_paths]

    matcher = {
        DeembedMethod.ORIGINAL: _match_original,
        DeembedMethod.GSG100: _match_gsg100,
        DeembedMethod.VZ: _match_vz,
        DeembedMethod.BASIC: _match_basic,
    }[method]

    open_name, short_name = matcher(dut_name, cal_names)

    if not open_name or not short_name:
        raise DeembedError(f"{method.value} 方法无法为 {dut_path.name} 匹配校准件")

    name_to_path = {p.name: p for p in cal_paths}
    return name_to_path[open_name], name_to_path[short_name]


def deembed(
    dut_path: str | Path,
    open_path: str | Path,
    short_path: str | Path,
    out_path: str | Path,
) -> Path:
    """对单个 DUT s1p 做 ShortOpen 去嵌，写出去嵌后的 s1p。

    返回 out_path（Path 对象）。
    """
    import skrf as rf
    from skrf.calibration.deembedding import ShortOpen

    dut = rf.Network(str(dut_path))
    op = rf.Network(str(open_path))
    sh = rf.Network(str(short_path))

    real_dut = ShortOpen(dummy_short=sh, dummy_open=op).deembed(dut)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    real_dut.write_touchstone(str(out_path).replace(".s1p", ""))
    return out_path


# ─────────────────────────── 批量去嵌 ───────────────────────────


def _run_deembed(
    s1p_pairs: list[tuple[Path, Path]],
    cal_open: dict[str, Path],
    cal_short: dict[str, Path],
    target_dir: Path,
    method: DeembedMethod = DeembedMethod.DEFAULT,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[tuple[Path, Path]]:
    """对每对 (S11, S22) s1p 用同目录 OPEN/SHORT s2p 做去嵌。

    步骤：
    1. 把每个 OPEN.s2p / SHORT.s2p 拆成 _S11.s1p / _S22.s1p
    2. 对每个 DUT 的 S11.s1p、S22.s1p 分别用对应端口的 OPEN/SHORT 去嵌
       （根据 method 选择匹配策略）
    3. 写出 *_de.s1p，返回新的 (s11_de, s22_de) 列表

    缺校准件直接 raise DeembedError，**不静默跳过**。
    """
    from app.core.touchstone import split_s2p_to_s1p

    if not cal_open or not cal_short:
        raise DeembedError(
            "已启用 De-embedding 但 ZIP 内未找到 OPEN/SHORT 校准 .s2p 文件；"
            "请确认压缩包包含同名 OPEN/SHORT 文件，或在上传时取消 De-embed 选项。"
        )

    cal_s11_dir = target_dir / "cal_S11"
    cal_s22_dir = target_dir / "cal_S22"
    de_s11_dir = target_dir / "S11_de"
    de_s22_dir = target_dir / "S22_de"

    # 1. 拆所有 OPEN/SHORT.s2p → s1p（统一放入列表，由 match_calibration 自行识别）
    cal_s11: list[Path] = []
    cal_s22: list[Path] = []
    for op in cal_open.values():
        split = split_s2p_to_s1p(op, out_dir_s11=cal_s11_dir, out_dir_s22=cal_s22_dir)
        cal_s11.append(split.s11_path)
        cal_s22.append(split.s22_path)
    for sh in cal_short.values():
        split = split_s2p_to_s1p(sh, out_dir_s11=cal_s11_dir, out_dir_s22=cal_s22_dir)
        cal_s11.append(split.s11_path)
        cal_s22.append(split.s22_path)

    # 2. 逐对 DUT 去嵌
    new_pairs: list[tuple[Path, Path]] = []
    for idx, (s11_path, s22_path) in enumerate(s1p_pairs, start=1):
        try:
            op11, sh11 = match_calibration(s11_path, cal_s11, method)
            op22, sh22 = match_calibration(s22_path, cal_s22, method)
        except Exception as exc:
            raise DeembedError(
                f"无法为 {s11_path.name} / {s22_path.name} 找到匹配的 OPEN/SHORT 校准件: {exc}"
            ) from exc

        s11_de = de_s11_dir / s11_path.name.replace(".s1p", "_de.s1p")
        s22_de = de_s22_dir / s22_path.name.replace(".s1p", "_de.s1p")
        try:
            deembed(s11_path, op11, sh11, s11_de)
            deembed(s22_path, op22, sh22, s22_de)
        except Exception as exc:  # pragma: no cover - skrf 异常透传
            raise DeembedError(f"De-embedding 失败 {s11_path.name}: {exc}") from exc
        new_pairs.append((s11_de, s22_de))
        if progress_callback:
            progress_callback(idx, len(s1p_pairs))

    return new_pairs
