"""原始来源读取。

各资料方以原格式交付：CSV、JSON Lines、SQLite（只读）。
本层只做读取和最基本的类型解析，原始字段一律保留文本，
``received`` 解析为到达批次序号，用于按批次重放。
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SOURCE_FILES = {
    "tickets": "tickets.csv",
    "gates": "gates.csv",
    "scans": "scans.jsonl",
    "withdrawals": "withdrawals.jsonl",
    "shifts": "shifts.csv",
    "coverage": "coverage.db",
    "headcounts": "headcounts.csv",
}


def batch_of(row: dict) -> int:
    """到达批次；没有 received 字段的基础资料（如 gates）视为第 0 批。"""
    return int(row.get("received") or 0)


def parse_time(text: str) -> datetime:
    """解析带时区的 ISO 时间；资料契约要求一律带 +08:00。"""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"时间缺少时区：{text!r}")
    return moment


def as_of(rows, batch: int):
    """按到达批次过滤：只保留不晚于 batch 收到的行。"""
    return [row for row in rows if batch_of(row) <= batch]


def _load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_db(path: Path) -> list[dict]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        return [dict(row) for row in source.execute(f'SELECT * FROM "{path.stem}"')]


@dataclass(frozen=True)
class SourceBundle:
    """一次交付的全部原始资料，行保持资料方原字段。"""

    tickets: tuple[dict, ...]
    gates: tuple[dict, ...]
    scans: tuple[dict, ...]
    withdrawals: tuple[dict, ...]
    shifts: tuple[dict, ...]
    coverage: tuple[dict, ...]
    headcounts: tuple[dict, ...]

    @property
    def batches(self) -> tuple[int, ...]:
        """全部来源出现过的到达批次，升序。"""
        seen = set()
        for rows in (
            self.tickets,
            self.gates,
            self.scans,
            self.withdrawals,
            self.shifts,
            self.coverage,
            self.headcounts,
        ):
            for row in rows:
                seen.add(batch_of(row))
        return tuple(sorted(seen))

    @property
    def latest_batch(self) -> int:
        return max(self.batches, default=0)


def load_sources(data_dir: Path | str) -> SourceBundle:
    """从资料目录读取全部来源。"""
    root = Path(data_dir)
    readers = {".csv": _load_csv, ".jsonl": _load_jsonl, ".db": _load_db}
    loaded = {}
    for name, filename in SOURCE_FILES.items():
        path = root / filename
        loaded[name] = tuple(readers[path.suffix](path))
    return SourceBundle(**loaded)
