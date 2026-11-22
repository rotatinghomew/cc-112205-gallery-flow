"""人流事实仓：按批次重放、签收交班、重新核查与影响提示。

- 全部事实由原始资料按到达批次重放而来，已签交班单记录当时的
  资料可见次序（批次），之后随时能按当时的资料查回。
- 新资料到达后重新核查受影响时段：结论有变化的班次提示受影响，
  新资料未涉及的远处场馆维持原签，无需撤签。
- 每条结论都能从总体解释到闸机原始凭据（扫描、撤销单、覆盖、清点）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .handover import ShiftVerdict, verify_all
from .replay import ReplayResult, describe_movement, replay
from .sources import SourceBundle, batch_of, load_sources

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signatures (
    signature_id TEXT PRIMARY KEY,
    shift TEXT NOT NULL,
    hall TEXT NOT NULL,
    as_of INTEGER NOT NULL,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL,
    signed_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class RecheckEntry:
    signature_id: str
    shift: str
    hall: str
    signed_batch: int
    integrity_ok: bool          # 按当时批次重放，结论与签收一致
    signed_verdict: str
    current_verdict: str | None
    changed: bool
    note: str


@dataclass(frozen=True)
class RecheckReport:
    as_of: int
    entries: tuple[RecheckEntry, ...]
    unsigned_shifts: tuple[str, ...]
    touched_halls: tuple[str, ...]   # 签收批次之后新资料直接触及的场馆

    @property
    def affected(self) -> tuple[RecheckEntry, ...]:
        return tuple(entry for entry in self.entries if entry.changed or not entry.integrity_ok)


class FactWarehouse:
    """人流事实仓。资料目录只读，签收记录写入本地 SQLite。"""

    def __init__(self, data_dir: Path | str = "data", store_path: Path | str = "warehouse.db"):
        self.data_dir = Path(data_dir)
        self.store_path = Path(store_path)
        self._sources: SourceBundle | None = None
        with sqlite3.connect(self.store_path) as store:
            store.execute(_SCHEMA)

    @property
    def sources(self) -> SourceBundle:
        if self._sources is None:
            self._sources = load_sources(self.data_dir)
        return self._sources

    def replay(self, as_of_batch: int | None = None) -> ReplayResult:
        return replay(self.sources, as_of_batch)

    def verdicts(self, as_of_batch: int | None = None) -> list[ShiftVerdict]:
        result = self.replay(as_of_batch)
        return verify_all(self.sources, result)

    # ---- 签收 ----

    def sign(self, shift_id: str, as_of_batch: int | None = None, signature_id: str | None = None) -> dict:
        """按当前（或指定）批次的资料签收一个班次的交班结论。"""
        batch = self.sources.latest_batch if as_of_batch is None else as_of_batch
        verdict = next((v for v in self.verdicts(batch) if v.shift == shift_id), None)
        if verdict is None:
            raise KeyError(f"批次 {batch} 下没有班次 {shift_id}")
        signature_id = signature_id or f"SIG-{shift_id}-{batch:02d}"
        record = {
            "signature_id": signature_id,
            "shift": verdict.shift,
            "hall": verdict.hall,
            "as_of": batch,
            "payload": verdict.payload(),
            "digest": verdict.digest,
            "signed_at": datetime.now(timezone.utc).isoformat(),
        }
        with sqlite3.connect(self.store_path) as store:
            store.execute(
                "INSERT INTO signatures VALUES (:signature_id, :shift, :hall, :as_of,"
                " :payload, :digest, :signed_at)",
                {**record, "payload": json.dumps(record["payload"], ensure_ascii=False, sort_keys=True)},
            )
        return record

    def signatures(self) -> list[dict]:
        with sqlite3.connect(self.store_path) as store:
            store.row_factory = sqlite3.Row
            rows = store.execute("SELECT * FROM signatures ORDER BY hall, shift, as_of").fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    # ---- 重新核查 ----

    def recheck(self, as_of_batch: int | None = None) -> RecheckReport:
        """对照已签记录重新核查：结论是否仍按当时资料可复现、是否被新资料改变。"""
        batch = self.sources.latest_batch if as_of_batch is None else as_of_batch
        current = {v.shift: v for v in self.verdicts(batch)}
        entries: list[RecheckEntry] = []
        signed_shifts = set()
        for record in self.signatures():
            signed_shifts.add(record["shift"])
            # 按签收时的批次重放，验证当时结论可复现
            replayed = {v.shift: v for v in self.verdicts(record["as_of"])}
            past = replayed.get(record["shift"])
            integrity_ok = past is not None and past.digest == record["digest"]
            now = current.get(record["shift"])
            changed = now is None or now.digest != record["digest"]
            if not integrity_ok:
                note = "签收结论按当时资料无法复现，需人工核查"
            elif changed:
                note = "受后续资料影响，结论已变，需复核并决定是否撤签"
            else:
                note = "结论未变，维持原签，无需撤签"
            entries.append(
                RecheckEntry(
                    signature_id=record["signature_id"],
                    shift=record["shift"],
                    hall=record["hall"],
                    signed_batch=record["as_of"],
                    integrity_ok=integrity_ok,
                    signed_verdict=record["payload"]["verdict"],
                    current_verdict=now.verdict if now else None,
                    changed=changed,
                    note=note,
                )
            )
        unsigned = tuple(sorted(set(current) - signed_shifts))
        touched = self._touched_halls_since(self._min_signed_batch(), batch)
        return RecheckReport(
            as_of=batch,
            entries=tuple(entries),
            unsigned_shifts=unsigned,
            touched_halls=touched,
        )

    def _min_signed_batch(self) -> int:
        batches = [record["as_of"] for record in self.signatures()]
        return min(batches) if batches else 0

    def _touched_halls_since(self, old_batch: int, new_batch: int) -> tuple[str, ...]:
        """两个批次之间新资料直接触及的场馆（含连廊对端）。"""
        sources = self.sources
        gates = {row["gate"]: row for row in sources.gates}
        halls: set[str] = set()

        def touch_gate(gate_id: str):
            gate = gates.get(gate_id)
            if gate is None:
                return
            halls.add(gate["hall"])
            if gate["other"] != "outside":
                halls.add(gate["other"])

        fresh = lambda rows: [r for r in rows if old_batch < batch_of(r) <= new_batch]
        for row in fresh(sources.scans):
            touch_gate(row["gate"])
        for row in fresh(sources.withdrawals):
            touch_gate(row["issuer"])
        for row in fresh(sources.coverage):
            touch_gate(row["gate"])
        for row in fresh(sources.headcounts):
            halls.add(row["hall"])
        for row in fresh(sources.shifts):
            halls.add(row["hall"])
        return tuple(sorted(halls))

    # ---- 解释 ----

    def explain(self, hall: str, as_of_batch: int | None = None) -> str:
        """从总体结论解释到闸机原始凭据。"""
        batch = self.sources.latest_batch if as_of_batch is None else as_of_batch
        result = self.replay(batch)
        verdicts = [v for v in verify_all(self.sources, result) if v.hall == hall]
        lines: list[str] = []
        for verdict in verdicts:
            lines.append(
                f"班次 {verdict.shift}（{hall} 馆，运营日 {verdict.operating_day}，"
                f"值班 {_hm(verdict.start)}–{_hm(verdict.handover)}）：{verdict.label}"
            )
            for reason in verdict.reasons:
                lines.append(f"  理由：{reason}")
            for cov in verdict.coverage:
                span = "、".join(f"{_hm(a)}–{_hm(b)}" for a, b in cov.intervals) or "无"
                mark = "完备" if cov.complete else "有缺口"
                lines.append(f"  门区 {cov.gate} 覆盖[{mark}]：{span}")
            if verdict.headcount.reported is not None:
                lines.append(f"  清点：{verdict.headcount.note}")
            for report in verdict.rights:
                lines.append(f"  通行权 {report.right.label}：{report.whereabouts}")
                for movement in result.rights[report.right].movements:
                    scans = "、".join(movement.scans)
                    lines.append(
                        f"    移动 {movement.kind} @{_hm(movement.time)}"
                        f"（{describe_movement(movement)}）凭扫描 {scans}"
                    )
                    for scan_id in movement.scans:
                        scan = result.scan_by_id(scan_id)
                        if scan is not None:
                            lines.append(
                                f"      原始凭据 {json.dumps(scan.raw, ensure_ascii=False, sort_keys=True)}"
                            )
        # 与本馆有关的撤销单、待核扫票
        related = [s for s in result.withdrawn if s.gate and s.gate.hall == hall]
        for scan in related:
            lines.append(
                f"  已撤销 {scan.scan_id}：{scan.withdrawal.get('reason', '')}"
                f"（撤销单批次 {scan.withdrawal.get('received')}，扫描到达批次 {scan.deliveries}）"
            )
        for scan in result.pending:
            if scan.gate and scan.gate.hall == hall:
                lines.append(f"  待核 {scan.scan_id}：{'；'.join(scan.notes)}")
        return "\n".join(lines) if lines else f"{hall} 馆在当前批次下没有班次"


def _hm(moment) -> str:
    return moment.strftime("%m-%d %H:%M")
