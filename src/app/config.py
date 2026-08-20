"""应用配置 + 算法配置。

- Settings：运行时环境变量（DATABASE_URL / REDIS_URL / DATA_ROOT 等）
- AlgorithmConfig：算法层魔数（min_separation / savgol 窗口 / mBVD 物理约束等）

魔数全部集中在此，不允许在算法实现里写硬编码常量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置（来自环境变量 / .env）。"""

    # 桌面版通过 DOTENV_PATH 指向打包进来的 .env；
    # 开发环境默认加载项目根目录（backend/..）下的 .env，避免从 backend/ 启动时找不到。
    _repo_root = Path(__file__).resolve().parent.parent.parent
    model_config = SettingsConfigDict(
        env_file=os.environ.get("DOTENV_PATH", str(_repo_root / ".env")),
        extra="ignore",
    )

    # 桌面模式
    ALN_DESKTOP_MODE: bool = False
    ALN_DESKTOP_DIR: Path | None = None

    # 数据库
    DATABASE_URL: str = "postgresql+psycopg://aln:aln@localhost:5432/aln"

    # 任务队列
    REDIS_URL: str = "redis://localhost:6379/0"

    # 数据根目录（容器内挂载点；本地开发可指向 /data3/aln）
    DATA_ROOT: Path = Path("/data3/aln")

    # 代理模型产物（数据集 npz/meta/baseline + 训练 checkpoint）；
    # 相对路径基于 backend/ 目录解析；缺失时 /api/surrogate/* 降级 available=false
    SURROGATE_DATA_DIR: Path = Path("surrogate_data")
    SURROGATE_CKPT_DIR: Path = Path("checkpoints_surrogate")

    # 上传限制
    UPLOAD_MAX_GB: int = 100

    # 目录监听上传
    WATCH_ENABLED: bool = True
    WATCH_DIR: Path | None = None  # 默认 = data_root / "watch"
    WATCH_DELETE_PROCESSED: bool = True  # 成功后是否删除原 zip

    # 数据保留策略
    KEEP_RAW_ZIP: bool = False  # 解压分析成功后是否保留原 zip

    # 边解压边计算流水线
    PIPELINE_ENABLED: bool = True  # 是否启用新链路
    PIPELINE_WORKERS: int = 0  # 0 = os.cpu_count()
    PIPELINE_SCAN_INTERVAL: float = 1.0  # 文件扫描间隔（秒）
    PIPELINE_COMPRESS_RAW: bool = True  # 提参后是否 gzip 原始 snp
    PIPELINE_KEEP_DEEMBED_TEMP: bool = False  # 是否保留去嵌中间 *_de.s1p

    # 日志
    LOG_LEVEL: str = "INFO"

    # 调试
    DEBUG: bool = False

    # 数据库连接池（默认适配 4 worker × 多线程 + Celery 并发）
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 30
    DB_POOL_RECYCLE: int = 3600  # 1 小时回收，防防火墙断连接
    DB_POOL_TIMEOUT: int = 30  # 等待连接池释放的超时秒数

    @property
    def is_desktop(self) -> bool:
        return self.ALN_DESKTOP_MODE

    @property
    def desktop_dir(self) -> Path:
        if self.ALN_DESKTOP_DIR is not None:
            return self.ALN_DESKTOP_DIR
        return Path.home() / ".aln-data"

    @property
    def data_root(self) -> Path:
        if self.is_desktop:
            return self.desktop_dir
        return self.DATA_ROOT

    @property
    def watch_dir(self) -> Path:
        if self.WATCH_DIR is not None:
            return self.WATCH_DIR
        return self.data_root / "watch"

    @property
    def uploads_dir(self) -> Path:
        return self.data_root / "uploads"

    @property
    def files_dir(self) -> Path:
        return self.data_root / "files"

    @property
    def mappings_dir(self) -> Path:
        return self.data_root / "mappings"

    @property
    def exports_dir(self) -> Path:
        return self.data_root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def resolved_database_url(self) -> str:
        if self.is_desktop:
            return f"sqlite:///{self.desktop_dir / 'aln-data.db'}"
        return self.DATABASE_URL

    @model_validator(mode="after")
    def _apply_desktop_database(self) -> Settings:
        """桌面模式下强制使用 SQLite 并将路径写入 DATABASE_URL。"""
        if self.is_desktop:
            self.DATABASE_URL = self.resolved_database_url
        return self


@dataclass
class AlgorithmConfig:
    """算法层魔数（全部从客户脚本里抽出来）。

    详见 docs/algorithm-port.md 第 4 节。
    """

    # ── 谐振峰检测 ────────────────────────────────────────────
    min_separation_hz: float = 20e6  # fs/fp 最小间距

    # ── BodeQ 平滑与拟合 ──────────────────────────────────────
    savgol_window: int = 51  # Savitzky-Golay 窗口（自动 cap 到 len(data)//10*2+1）
    savgol_polyorder: int = 3
    lorentz_peak_range_ratio: float = 0.3  # 拟合带宽 = 总点数 × 该比例（前后各取）

    # ── 中间寄生峰检测 ────────────────────────────────────────
    intermediate_peak_prominence_db: float = 3.0
    intermediate_peak_smooth_window_ratio: float = 0.01
    intermediate_peak_min_valley_sep_ratio: float = 0.02

    # ── BodeQ 边界 ────────────────────────────────────────────
    bodeq_boundary_ratio: float = 0.05  # 前后各裁掉 5% 防边界拟合崩坏

    # ── 阻抗下限（避免 log(0)） ──────────────────────────────
    z_db_floor: float = 1e-12

    # ── 并发 ──────────────────────────────────────────────────
    threadpool_max_workers: int = 4
    # Celery 任务内参数提取的并行 worker 数。
    # 0 或 1 表示禁用（单线程）；>1 启用 multiprocessing。
    # 注意：在 macOS 开发环境建议设为 1，Linux 生产环境可设为 CPU 核心数。
    worker_extract_workers: int = 1

    # ── 数据清洗 ──────────────────────────────────────────────
    # 任一列为 NA/空 → 整行丢弃（来自需求文档 3.1）
    required_numeric_columns: tuple[str, ...] = field(
        default_factory=lambda: (
            "fs_ghz",
            "fp_ghz",
            "zs_ohm",
            "zp_ohm",
            "qs",
            "qp",
            "qs_bodeq",
            "qp_bodeq",
            "dbqs",
            "dbqp",
            "bodeq_fitted",
            "bodeq_smooth",
            "fbode_ghz",
            "k2eff_pct",
        )
    )

    # ── 代理模型（surrogate）──────────────────────────────────
    surr_n_freq: int = 1001  # 统一频点数
    surr_k: int = 256  # 稀疏采样点数
    surr_dense_ratio: float = 0.60  # 谐振密集区配额
    surr_transition_ratio: float = 0.25  # 过渡带配额（远带 = 1 - 前两者）
    surr_dense_half_bw: float = 0.5  # 密集区半宽 = 该值 × (fp - fs)
    surr_transition_half_bw: float = 1.5  # 过渡带外沿 = 该值 × (fp - fs)
    surr_clean_fs_tol_mhz: float = 100.0  # 单元内 fs/fp 离群阈值（MHz；比较时 ÷1000 换算为 GHz）
    surr_clean_trend_sigma: float = 3.0  # 跨单元趋势面离群阈值（σ 倍数）
    surr_split_seed: int = 42  # 划分随机种子
    surr_param_cols: tuple[str, ...] = (  # 7 个性能参数列（顺序固定，全流程一致）
        "fs_ghz",
        "fp_ghz",
        "k2eff_pct",
        "qs",
        "qp",
        "qs_bodeq",
        "qp_bodeq",
    )
    surr_per_cell_max: int = 5  # 每结构组合抽样上限（|fs-中位数| 升序取前 N）
    surr_default_ag: float = 1.0  # AG 占位默认值（μm；数据全空时的常量输入）
    surr_val_groups: int = 29  # val 物理器件组数
    surr_test_groups: int = 29  # test 物理器件组数


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_algorithm_config() -> AlgorithmConfig:
    return AlgorithmConfig()
