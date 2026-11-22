"""校核可交班范围。

按值班区间（运营日边界，跨午夜不清零）逐馆核对：

- 门区覆盖：值班确认的覆盖区间（含头不含尾）必须盖住整个值班范围，
  缺一段就是缺这道门的证据。
- 通行权去向：区间内触及本馆的通行权，交班时刻去向必须确定；
  只过了一端连廊或记录相斥的，保留为未证实并挡住相关场馆。
- 待核扫票与无令牌共用人次：都意味着实际占用的可能，不能抹掉。
- 值班清点：覆盖完整且与已核序列匹配才佐证时段；冲突时如实报告
  两个数，不把差额均摊到人次上。

结论分四档：可交班（READY）、仍有人在场不能交班（OCCUPIED）、
暂定（TENTATIVE）、缺证据（INSUFFICIENT）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .identity import RightKey
from .replay import ReplayResult, S_CONTESTED, S_IN, S_OUTSIDE, S_TRANSIT
from .sources import SourceBundle, as_of, batch_of, parse_time

READY = "READY"
OCCUPIED = "OCCUPIED"
TENTATIVE = "TENTATIVE"
INSUFFICIENT = "INSUFFICIENT"

VERDICT_LABELS = {
    READY: "可交班",
    OCCUPIED: "不能交班（仍有人在场）",
    TENTATIVE: "暂定",
    INSUFFICIENT: "缺证据",
}


@dataclass(frozen=True)
class GateCoverage:
    gate: str
    intervals: tuple[tuple[object, object], ...]
    gaps: tuple[tuple[object, object], ...]

    @property
    def complete(self) -> bool:
        return not self.gaps


@dataclass(frozen=True)
class RightReport:
    right: RightKey
    whereabouts: str
    resolved: bool
    blocking_halls: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class HeadcountCheck:
    status: str          # corroborated / conflict / missing / reference
    reported: int | None
    derived: int
    note: str


@dataclass(frozen=True)
class ShiftVerdict:
    shift: str
    hall: str
    start: object
    handover: object
    operating_day: str
    as_of: int
    verdict: str
    reasons: tuple[str, ...]
    certain: tuple[RightKey, ...]
    possible: tuple[RightKey, ...]
    anonymous: int
    coverage: tuple[GateCoverage, ...]
    rights: tuple[RightReport, ...]
    headcount: HeadcountCheck

    @property
    def label(self) -> str:
        return VERDICT_LABELS[self.verdict]

    def payload(self) -> dict:
        return {
            "shift": self.shift,
            "hall": self.hall,
            "as_of": self.as_of,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "certain": sorted(key.label for key in self.certain),
            "possible": sorted(key.label for key in self.possible),
            "anonymous": self.anonymous,
            "headcount": {
                "status": self.headcount.status,
                "reported": self.headcount.reported,
                "derived": self.headcount.derived,
            },
            "coverage_gaps": {
                cov.gate: [[a.isoformat(), b.isoformat()] for a, b in cov.gaps]
                for cov in self.coverage
                if cov.gaps
            },
        }

    @property
    def digest(self) -> str:
        # 结论指纹不含批次号：批次只记录资料可见次序，不属于结论本身
        payload = {k: v for k, v in self.payload().items() if k != "as_of"}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _coverage_gaps(intervals, start, end):
    """值班范围 [start, end) 内未被覆盖的区间（覆盖含头不含尾）。"""
    gaps = []
    cursor = start
    for lo, hi in sorted(intervals):
        if hi <= cursor:
            continue
        if lo > cursor:
            gaps.append((cursor, min(lo, end)))
        cursor = max(cursor, hi)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return tuple(gaps)


def _fmt(moment) -> str:
    return moment.strftime("%m-%d %H:%M")


def verify_shift(sources: SourceBundle, result: ReplayResult, shift_row: dict) -> ShiftVerdict:
    hall = shift_row["hall"]
    start = parse_time(shift_row["from"])
    handover = parse_time(shift_row["handover"])
    as_of_batch = result.as_of

    # 1) 门区覆盖
    coverage_rows = [r for r in as_of(sources.coverage, as_of_batch)]
    coverages: list[GateCoverage] = []
    insufficient: list[str] = []
    for gate in sorted((g for g in result.gates.values() if g.hall == hall), key=lambda g: g.gate):
        intervals = [
            (parse_time(r["from"]), parse_time(r["to"]))
            for r in coverage_rows
            if r["gate"] == gate.gate
        ]
        gaps = _coverage_gaps(intervals, start, handover)
        coverages.append(GateCoverage(gate=gate.gate, intervals=tuple(intervals), gaps=gaps))
        for lo, hi in gaps:
            insufficient.append(f"缺门区证据：{gate.gate} 缺 {_fmt(lo)}–{_fmt(hi)} 的完备覆盖")
    coverage_complete = all(cov.complete for cov in coverages)

    # 2) 通行权去向
    snapshot = result.presence_at(handover, [hall])
    reports: list[RightReport] = []
    for key in result.rights_touching(hall, start, handover):
        span = result.rights[key].state_at(handover)
        if span.state == S_IN:
            whereabouts = f"在 {span.hall} 馆"
            resolved, blocking = True, ()
        elif span.state == S_OUTSIDE:
            whereabouts = "已离馆"
            resolved, blocking = True, ()
        elif span.state == S_TRANSIT:
            whereabouts = f"已离开 {span.origin}，目的地 {span.candidate} 未证实"
            resolved, blocking = False, tuple(h for h in (span.origin, span.candidate) if h)
        elif span.state == S_CONTESTED:
            halls = sorted(h for h in (span.candidates or ()) if h)
            whereabouts = "记录相斥，候选：" + ("、".join(halls + ["馆外"]))
            touched = {
                m.from_hall or m.to_hall or m.candidate_hall
                for m in result.rights[key].movements
                if start <= m.time <= handover
            }
            blocking = tuple(sorted((set(halls) | {h for h in touched if h}) & set(_hall_codes(result))))
            resolved = False
        else:
            whereabouts = "无记录"
            resolved, blocking = True, ()
        reports.append(
            RightReport(
                right=key,
                whereabouts=whereabouts,
                resolved=resolved,
                blocking_halls=blocking,
                detail="；".join(result.rights[key].notes),
            )
        )
        if not resolved and hall in blocking:
            insufficient.append(f"缺通行权证据：{key.label} {whereabouts}")

    # 3) 待核扫票：不合法但不能抹去实际占用的可能
    for scan in result.pending:
        if scan.gate is None or scan.gate.hall != hall:
            continue
        if start <= scan.time <= handover:
            insufficient.append(f"待核扫票：{scan.scan_id}（{'；'.join(scan.notes)}）")

    # 4) 无令牌共用人次
    anonymous = snapshot.anonymous.get(hall, 0)
    if anonymous:
        insufficient.append(f"无令牌共用人次 {anonymous} 人在场可能无法拆分到个人")

    # 5) 值班清点
    derived = len(snapshot.certain_in(hall))
    headcount = _check_headcount(sources, hall, handover, derived, snapshot, anonymous, coverage_complete, as_of_batch)

    # 6) 结论
    certain = snapshot.certain_in(hall)
    possible = snapshot.possible_in(hall)
    reasons = list(insufficient)
    if insufficient:
        verdict = INSUFFICIENT
    elif certain:
        verdict = OCCUPIED
        reasons.append("仍有人在场：" + "、".join(sorted(key.label for key in certain)))
        if headcount.status == "conflict":
            reasons.append(headcount.note)
    elif headcount.status in ("missing", "conflict"):
        verdict = TENTATIVE
        reasons.append(headcount.note)
    else:
        verdict = READY
        reasons.append("门区覆盖完整，在场已核清" + ("，清点佐证一致" if headcount.status == "corroborated" else ""))

    return ShiftVerdict(
        shift=shift_row["shift"],
        hall=hall,
        start=start,
        handover=handover,
        operating_day=start.date().isoformat(),
        as_of=as_of_batch,
        verdict=verdict,
        reasons=tuple(reasons),
        certain=tuple(sorted(certain, key=lambda k: k.label)),
        possible=tuple(sorted(possible, key=lambda k: k.label)),
        anonymous=anonymous,
        coverage=tuple(coverages),
        rights=tuple(reports),
        headcount=headcount,
    )


def _hall_codes(result: ReplayResult):
    return {gate.hall for gate in result.gates.values()}


def _check_headcount(sources, hall, handover, derived, snapshot, anonymous, coverage_complete, as_of_batch) -> HeadcountCheck:
    rows = [
        r for r in as_of(sources.headcounts, as_of_batch)
        if r["hall"] == hall and parse_time(r["time"]) == handover
    ]
    if not rows:
        return HeadcountCheck(status="missing", reported=None, derived=derived,
                              note="清点缺失：无交班时刻的值班清点，结论仅暂定")
    latest = max(rows, key=batch_of)
    reported = int(latest["count"])
    if not coverage_complete:
        return HeadcountCheck(status="reference", reported=reported, derived=derived,
                              note=f"覆盖不完整，清点 {reported} 仅作参考")
    if snapshot.possible_in(hall) or anonymous:
        return HeadcountCheck(status="reference", reported=reported, derived=derived,
                              note=f"存在未证实的在场可能，清点 {reported} 仅作参考")
    if reported == derived:
        return HeadcountCheck(status="corroborated", reported=reported, derived=derived,
                              note=f"清点 {reported} 与已核序列一致")
    return HeadcountCheck(status="conflict", reported=reported, derived=derived,
                          note=f"清点冲突：值班清点 {reported}，闸机已核 {derived}（差额不得均摊到人次）")


def verify_all(sources: SourceBundle, result: ReplayResult) -> list[ShiftVerdict]:
    """核对当前批次下全部已到班次的交班结论。"""
    return [
        verify_shift(sources, result, row)
        for row in sorted(as_of(sources.shifts, result.as_of), key=lambda r: (r["hall"], r["from"]))
    ]
