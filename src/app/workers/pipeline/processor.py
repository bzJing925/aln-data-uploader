"""Single-DUT processor: split / de-embed / extract / archive.

Orchestrates the per-item pipeline:
- S2P: split to S11/S22, de-embed, extract params for each port
- S1P: direct param extraction
- Gzip raw files on success (optional)
- Clean up de-embed temp files (optional)
"""

from __future__ import annotations

import gzip
import logging
import shutil
from pathlib import Path

from app.config import AlgorithmConfig
from app.core.deembed import DeembedError, deembed
from app.core.extract import ExtractError, extract_resonator_params
from app.core.touchstone import split_s2p_to_s1p
from app.workers.pipeline.calibration import CalibrationIndex

logger = logging.getLogger(__name__)


class DutProcessor:
    """Process a single DUT item (S1P or S2P) through the full pipeline."""

    def __init__(
        self,
        compress_raw: bool = True,
        keep_deembed_temp: bool = False,
        config: AlgorithmConfig | None = None,
    ) -> None:
        self.compress_raw = compress_raw
        self.keep_deembed_temp = keep_deembed_temp
        self.config = config

    def process(
        self,
        item: dict,
        mapping: dict | None,
        wafer: int | None,
        cal_index: CalibrationIndex | None,
        target_dir: str | Path,
    ) -> dict:
        """Process one DUT item and return results.

        Args:
            item: dict with "type" ("s1p" | "s2p") and "path" (str).
            mapping: Optional mark -> MappingEntry dict.
            wafer: Optional wafer number.
            cal_index: Optional CalibrationIndex for de-embedding.
            target_dir: Directory for intermediate and output files.

        Returns:
            {"ok": bool, "rows": [...], "failures": [...], "archived": [...]}
        """
        target_dir = Path(target_dir)
        item_type = item.get("type", "")
        item_path_str = item.get("path")
        if not item_path_str:
            return {
                "ok": False,
                "rows": [],
                "failures": [f"missing path for item with type '{item_type}'"],
                "archived": [],
            }
        item_path = Path(item_path_str)
        s_param_relpath = item.get("s_param_relpath") or str(item_path.name)

        rows: list[dict] = []
        failures: list[str] = []
        archived: list[str] = []

        if item_type == "s2p":
            self._process_s2p(
                item_path,
                mapping,
                wafer,
                cal_index,
                target_dir,
                s_param_relpath,
                rows,
                failures,
                archived,
            )
        elif item_type == "s1p":
            self._process_s1p(
                item_path,
                mapping,
                wafer,
                target_dir,
                s_param_relpath,
                rows,
                failures,
                archived,
                deembedded=False,
            )
        else:
            failures.append(f"{item_path.name}: unknown type '{item_type}'")

        ok = len(rows) > 0
        return {"ok": ok, "rows": rows, "failures": failures, "archived": archived}

    def _process_s1p(
        self,
        s1p_path: Path,
        mapping: dict | None,
        wafer: int | None,
        target_dir: Path,
        s_param_relpath: str,
        rows: list[dict],
        failures: list[str],
        archived: list[str],
        deembedded: bool = False,
        port: int = 0,
    ) -> None:
        """Extract params from a single S1P file."""
        try:
            row = extract_resonator_params(
                s1p_path,
                mapping=mapping,
                wafer=wafer,
                s_param_relpath=s_param_relpath,
                deembedded=deembedded,
                skip_validation=True,
                port=port,
                config=self.config,
            )
            if isinstance(row, dict):
                rows.append(row)
            else:
                rows.append(row.model_dump())
        except ExtractError as exc:
            failures.append(f"{s1p_path.name}: {exc}")
            return

        if self.compress_raw:
            gz_path = self._gzip_file(s1p_path)
            if gz_path:
                archived.append(str(gz_path))

    def _process_s2p(
        self,
        s2p_path: Path,
        mapping: dict | None,
        wafer: int | None,
        cal_index: CalibrationIndex | None,
        target_dir: Path,
        s_param_relpath: str,
        rows: list[dict],
        failures: list[str],
        archived: list[str],
    ) -> None:
        """Split S2P to S11/S22, optionally de-embed, then extract params."""
        s11_dir = target_dir / "S11"
        s22_dir = target_dir / "S22"

        try:
            split = split_s2p_to_s1p(s2p_path, out_dir_s11=s11_dir, out_dir_s22=s22_dir)
        except Exception as exc:
            failures.append(f"{s2p_path.name}: split failed: {exc}")
            # Clean up empty directories created by split_s2p_to_s1p
            if s11_dir.exists() and not any(s11_dir.iterdir()):
                s11_dir.rmdir()
            if s22_dir.exists() and not any(s22_dir.iterdir()):
                s22_dir.rmdir()
            return

        ports = [
            ("S11", split.s11_path, 0),
            ("S22", split.s22_path, 1),
        ]

        de_temp_files: list[Path] = []
        s2p_archived = False

        for port_name, s1p_path, port_idx in ports:
            try:
                if cal_index is not None:
                    open_path, short_path = cal_index.match(port_name, s1p_path)
                    de_path = s1p_path.with_name(f"{s1p_path.stem}_de.s1p")
                    deembed(s1p_path, open_path, short_path, de_path)
                    s1p_path = de_path
                    de_temp_files.append(de_path)
                    deembedded = True
                else:
                    deembedded = False

                row = extract_resonator_params(
                    s1p_path,
                    mapping=mapping,
                    wafer=wafer,
                    s_param_relpath=s_param_relpath,
                    deembedded=deembedded,
                    skip_validation=True,
                    port=port_idx,
                    config=self.config,
                )
                if isinstance(row, dict):
                    row_dict = row
                else:
                    row_dict = row.model_dump()
                row_dict["s_param_port"] = port_name
                rows.append(row_dict)
            except (ExtractError, DeembedError) as exc:
                failures.append(f"{s2p_path.name} ({port_name}): {exc}")
                continue

            s2p_archived = True

        # Archive original S2P once if any port succeeded
        if self.compress_raw and s2p_archived:
            gz_path = self._gzip_file(s2p_path)
            if gz_path:
                archived.append(str(gz_path))

        if not self.keep_deembed_temp:
            for temp_path in de_temp_files:
                if temp_path.exists():
                    temp_path.unlink()

    def _gzip_file(self, src: Path) -> Path | None:
        """Gzip a file and remove the original. Return the .gz path."""
        gz_path = src.with_suffix(src.suffix + ".gz")
        try:
            with open(src, "rb") as f_in:
                with gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            src.unlink()
            return gz_path
        except Exception as exc:
            logger.warning("gzip failed for %s: %s", src, exc)
            if gz_path.exists():
                gz_path.unlink()
            return None
