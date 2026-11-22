"""重放实际通行。

把闸机原始事实整理成运营要用的两层事实：

1. 移动：馆外进出、连廊双端配对的确定移动、只有一端的出/入。
   跨馆对开的闸机报同一通行号（pass）时，身份、方向、拓扑都相符的
   两端才算一笔确定移动；只有一端或两端相斥时，保留确定的一侧影响，
   去向/来路记为未证实，不用同团体票的另一人补齐。
2. 在场时间线：按通行权重放移动，得到任意时刻的在场状态。
   已过连廊边界但去向未证实的访客，同时保留「已离开原馆／目的地
   未证实」；记录相斥时保留各候选状态。时间线连续，跨午夜不清零。

闸机事实本身分三态：valid（有效）、withdrawn（闸机方正式撤销，
同号扫描之后再到也不直接复活）、pending（不合法扫票，保留待核，
不抹去报告和实际占用的可能）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .identity import IdentityBook, RightKey
from .sources import SourceBundle, as_of, batch_of, parse_time

OUTSIDE = "outside"

# 在场状态
S_UNKNOWN = "unknown"      # 尚无记录
S_OUTSIDE = "outside"      # 馆外
S_IN = "in"                # 确定在某馆
S_TRANSIT = "transit"      # 已离开原馆，目的地未证实
S_CONTESTED = "contested"  # 记录相斥，保留候选


@dataclass(frozen=True)
class GateFact:
    gate: str
    hall: str
    link: str
    other: str
    raw: dict

    @property
    def is_corridor(self) -> bool:
        """连廊门：另一端是馆内而不是馆外。"""
        return self.other != OUTSIDE


@dataclass
class ScanFact:
    """一条闸机扫描及其核对结论。"""

    scan_id: str
    gate: GateFact
    direction: str
    ticket_key: str
    token: str
    pass_id: str
    time: object
    batch: int
    deliveries: tuple[int, ...]
    raw: dict
    status: str = "valid"          # valid / withdrawn / pending
    notes: list[str] = field(default_factory=list)
    right: RightKey | None = None  # None 表示无令牌的共用人次
    withdrawal: dict | None = None


@dataclass(frozen=True)
class Movement:
    """一次移动事实。from/to 为 None 的一侧表示馆外或未证实。"""

    kind: str  # outside_enter / outside_exit / corridor_confirmed / corridor_exit_only / corridor_enter_only
    right: RightKey | None
    time: object
    from_hall: str | None
    to_hall: str | None
    candidate_hall: str | None
    scans: tuple[str, ...]
    pass_id: str
    note: str = ""


@dataclass(frozen=True)
class ConflictFact:
    """相斥记录：同一通行号两端身份不符，或同一通行权时间线自相矛盾。"""

    kind: str
    scans: tuple[str, ...]
    right: RightKey | None
    detail: str


def describe_movement(movement: Movement) -> str:
    """移动的人读描述：连廊单端的未证实一侧标候选馆，馆外门标馆外。"""
    origin = movement.from_hall or (
        f"{movement.candidate_hall}（来路未证实）" if movement.candidate_hall else "馆外"
    )
    destination = movement.to_hall or (
        f"{movement.candidate_hall}（去向未证实）" if movement.candidate_hall else "馆外"
    )
    return f"{origin}→{destination}"


@dataclass(frozen=True)
class PresenceSpan:
    """通行权在某区间内的在场状态。"""

    state: str
    since: object | None
    until: object | None
    hall: str | None = None                 # S_IN
    origin: str | None = None               # S_TRANSIT 已离开的原馆
    candidate: str | None = None            # S_TRANSIT 未证实的目的地
    candidates: frozenset | None = None     # S_CONTESTED（None 元素表示馆外）
    basis: tuple[str, ...] = ()
    note: str = ""

    def involves(self, hall: str) -> bool:
        if self.state == S_IN:
            return self.hall == hall
        if self.state == S_TRANSIT:
            return hall in (self.origin, self.candidate)
        if self.state == S_CONTESTED:
            return hall in (self.candidates or ())
        return False


@dataclass
class RightReplay:
    """一条通行权的重放结果。"""

    key: RightKey
    movements: list[Movement] = field(default_factory=list)
    spans: list[PresenceSpan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def state_at(self, moment) -> PresenceSpan:
        current = PresenceSpan(state=S_UNKNOWN, since=None, until=None)
        for span in self.spans:
            if span.since is not None and span.since > moment:
                break
            current = span
        return current

    @property
    def final(self) -> PresenceSpan:
        return self.spans[-1] if self.spans else PresenceSpan(state=S_UNKNOWN, since=None, until=None)


@dataclass(frozen=True)
class PresenceSnapshot:
    """某时刻全馆在场：确定在场、可能在场、无法拆分的共用人数。"""

    certain: dict[str, tuple[RightKey, ...]]
    possible: dict[str, tuple[RightKey, ...]]
    anonymous: dict[str, int]

    def certain_in(self, hall: str) -> tuple[RightKey, ...]:
        return self.certain.get(hall, ())

    def possible_in(self, hall: str) -> tuple[RightKey, ...]:
        return self.possible.get(hall, ())


def _build_gates(sources: SourceBundle) -> dict[str, GateFact]:
    return {
        row["gate"]: GateFact(
            gate=row["gate"], hall=row["hall"], link=row["link"], other=row["other"], raw=row
        )
        for row in sources.gates
    }


def _build_scans(sources: SourceBundle, gates: dict[str, GateFact], identity: IdentityBook, as_of_batch: int):
    """登记扫描事实：同号去重、撤销优先、合法性待核。"""
    withdrawals = {}
    for row in as_of(sources.withdrawals, as_of_batch):
        withdrawals.setdefault(row["scan"], []).append(row)

    scans: list[ScanFact] = []
    by_id: dict[str, ScanFact] = {}
    for row in sorted(as_of(sources.scans, as_of_batch), key=lambda r: (batch_of(r), r["scan"])):
        scan_id = row["scan"]
        if scan_id in by_id:
            # 同号补报：只追加到达批次，不另立事实；内容有出入时留痕待查
            first = by_id[scan_id]
            first.deliveries = tuple(sorted(first.deliveries + (batch_of(row),)))
            changed = [
                field
                for field in ("gate", "direction", "ticket", "token", "pass", "time")
                if str(row.get(field, "")) != str(first.raw.get(field, ""))
            ]
            if changed:
                first.notes.append(f"同号补报内容不一致（{'、'.join(changed)}），以首次到达为准")
            continue
        gate = gates.get(row["gate"])
        fact = ScanFact(
            scan_id=scan_id,
            gate=gate,
            direction=row["direction"],
            ticket_key=row["ticket"],
            token=row["token"],
            pass_id=row.get("pass", "") or "",
            time=parse_time(row["time"]),
            batch=batch_of(row),
            deliveries=(batch_of(row),),
            raw=row,
        )
        by_id[scan_id] = fact
        scans.append(fact)

    for fact in scans:
        if fact.scan_id in withdrawals:
            fact.status = "withdrawn"
            fact.withdrawal = withdrawals[fact.scan_id][-1]
            fact.notes.append(f"闸机方正式撤销：{fact.withdrawal.get('reason', '')}")
            continue
        _check_legality(fact, identity)
        if fact.status == "valid" and fact.token:
            fact.right = identity.ticket_right.get(fact.ticket_key)
            # 无令牌：同一凭据的共用人次，不落到个人通行权
    return scans


def _check_legality(fact: ScanFact, identity: IdentityBook) -> None:
    """不合法的扫票保留待核，不进入移动序列，但也不抹去。"""
    problems = []
    if fact.gate is None:
        problems.append(f"未知闸机 {fact.raw.get('gate')}")
    ticket = identity.tickets.get(fact.ticket_key)
    if ticket is None:
        problems.append(f"票证 {fact.ticket_key} 未登记")
    else:
        if ticket.token != fact.token:
            problems.append(f"令牌与票证登记不符（{fact.token}≠{ticket.token}）")
        if not (ticket.valid_from <= fact.time < ticket.valid_to):
            problems.append("扫票时刻不在票务有效期内")
        if fact.gate is not None and fact.gate.hall not in ticket.allowed:
            problems.append(f"场馆 {fact.gate.hall} 不在凭据可通行范围")
        if (
            fact.gate is not None
            and fact.gate.is_corridor
            and fact.gate.other not in ticket.allowed
        ):
            problems.append(f"连廊对端 {fact.gate.other} 不在凭据可通行范围")
    if fact.direction not in ("enter", "exit"):
        problems.append(f"未知方向 {fact.direction}")
    if problems:
        fact.status = "pending"
        fact.notes.extend(problems)


def _pair_movements(scans: list[ScanFact]):
    """连廊双端配对：同一通行号、身份、方向、拓扑、时刻都相符才成一笔。"""
    movements: list[Movement] = []
    conflicts: list[ConflictFact] = []

    corridor = [s for s in scans if s.status == "valid" and s.gate is not None and s.gate.is_corridor]
    by_pass: dict[str, list[ScanFact]] = {}
    for scan in corridor:
        by_pass.setdefault(scan.pass_id, []).append(scan)

    paired: set[str] = set()
    for pass_id, group in sorted(by_pass.items()):
        group.sort(key=lambda s: s.scan_id)
        exits = [s for s in group if s.direction == "exit"]
        enters = [s for s in group if s.direction == "enter"]
        for exit_scan in exits:
            if exit_scan.scan_id in paired:
                continue
            for enter_scan in enters:
                if enter_scan.scan_id in paired:
                    continue
                topo = (
                    exit_scan.gate.other == enter_scan.gate.hall
                    and enter_scan.gate.other == exit_scan.gate.hall
                    and exit_scan.gate.link == enter_scan.gate.link
                )
                if not topo or exit_scan.time != enter_scan.time:
                    continue
                if pass_id and exit_scan.right is not None and exit_scan.right == enter_scan.right:
                    paired.add(exit_scan.scan_id)
                    paired.add(enter_scan.scan_id)
                    movements.append(
                        Movement(
                            kind="corridor_confirmed",
                            right=exit_scan.right,
                            time=exit_scan.time,
                            from_hall=exit_scan.gate.hall,
                            to_hall=enter_scan.gate.hall,
                            candidate_hall=None,
                            scans=(exit_scan.scan_id, enter_scan.scan_id),
                            pass_id=pass_id,
                            note="双端确定的跨馆移动",
                        )
                    )
                    break
                # 拓扑、方向、时刻都相符但身份不符：两端相斥
                if pass_id and exit_scan.right != enter_scan.right:
                    conflicts.append(
                        ConflictFact(
                            kind="两端身份相斥",
                            scans=(exit_scan.scan_id, enter_scan.scan_id),
                            right=None,
                            detail=(
                                f"通行号 {pass_id} 一端报 {exit_scan.ticket_key}/{exit_scan.token}、"
                                f"另一端报 {enter_scan.ticket_key}/{enter_scan.token}，各端只保留自身影响"
                            ),
                        )
                    )

    for scan in corridor:
        if scan.scan_id in paired:
            continue
        if scan.direction == "exit":
            movements.append(
                Movement(
                    kind="corridor_exit_only",
                    right=scan.right,
                    time=scan.time,
                    from_hall=scan.gate.hall,
                    to_hall=None,
                    candidate_hall=scan.gate.other,
                    scans=(scan.scan_id,),
                    pass_id=scan.pass_id,
                    note="只有本端报到：已离开原馆，目的地未证实",
                )
            )
        else:
            movements.append(
                Movement(
                    kind="corridor_enter_only",
                    right=scan.right,
                    time=scan.time,
                    from_hall=None,
                    to_hall=scan.gate.hall,
                    candidate_hall=scan.gate.other,
                    scans=(scan.scan_id,),
                    pass_id=scan.pass_id,
                    note="只有本端报到：已进入本馆，来路未证实",
                )
            )

    for scan in scans:
        if scan.status != "valid" or scan.gate is None or scan.gate.is_corridor:
            continue
        kind = "outside_enter" if scan.direction == "enter" else "outside_exit"
        movements.append(
            Movement(
                kind=kind,
                right=scan.right,
                time=scan.time,
                from_hall=None if scan.direction == "enter" else scan.gate.hall,
                to_hall=scan.gate.hall if scan.direction == "enter" else None,
                candidate_hall=None,
                scans=(scan.scan_id,),
                pass_id=scan.pass_id,
            )
        )

    movements.sort(key=lambda m: (m.time, m.scans))
    return movements, conflicts


class _PresenceMachine:
    """单条通行权的在场状态机。每条闸机事实本身确定，冲突只来自记录之间。"""

    def __init__(self, key: RightKey):
        self.replay = RightReplay(key=key)
        self.state = S_UNKNOWN
        self.hall = None
        self.origin = None
        self.candidate = None
        self.candidates = None

    def _push(self, moment, scans, note=""):
        self.replay.spans.append(
            PresenceSpan(
                state=self.state,
                since=moment,
                until=None,
                hall=self.hall,
                origin=self.origin,
                candidate=self.candidate,
                candidates=self.candidates,
                basis=tuple(scans),
                note=note,
            )
        )

    def _set(self, state, moment, scans, hall=None, origin=None, candidate=None, candidates=None, note=""):
        self.state, self.hall, self.origin, self.candidate, self.candidates = (
            state, hall, origin, candidate, candidates,
        )
        self._push(moment, scans, note)

    def _contest(self, candidates, moment, scans, note):
        """记录相斥：保留各候选状态，等待资料修正而不是擅自取舍。"""
        if self.state == S_CONTESTED:
            merged = set(self.candidates or ()) | set(candidates)
            self.candidates = frozenset(merged)
            self.replay.notes.append(note)
            self._push(moment, scans, note)
            return
        self._set(S_CONTESTED, moment, scans, hall=None, candidates=frozenset(candidates), note=note)
        self.replay.notes.append(note)

    def apply(self, movement: Movement):
        kind = movement.kind
        if kind == "outside_enter":
            if self.state in (S_UNKNOWN, S_OUTSIDE):
                self._set(S_IN, movement.time, movement.scans, hall=movement.to_hall)
            elif self.state == S_IN:
                self._contest({movement.to_hall, self.hall}, movement.time, movement.scans,
                              "入馆记录与仍在上一馆的在场状态相斥")
            else:
                self._contest({movement.to_hall, self.candidate}, movement.time, movement.scans,
                              "入馆记录与未证实的去向相斥")
        elif kind == "outside_exit":
            hall = movement.from_hall
            if self.state == S_IN and self.hall == hall:
                self._set(S_OUTSIDE, movement.time, movement.scans)
            elif self.state == S_UNKNOWN:
                self._set(S_OUTSIDE, movement.time, movement.scans, note="离场前无入场记录")
            elif self.state == S_OUTSIDE:
                self.replay.notes.append(f"重复离场记录 {movement.scans}")
            elif self.state == S_IN:
                self._contest({None, self.hall}, movement.time, movement.scans,
                              f"从 {hall} 离场与仍在 {self.hall} 的在场状态相斥")
            else:  # TRANSIT / CONTESTED
                self._contest({None, self.candidate}, movement.time, movement.scans,
                              f"从 {hall} 离场与未证实的去向相斥")
        elif kind == "corridor_confirmed":
            if self.state == S_IN and self.hall == movement.from_hall:
                self._set(S_IN, movement.time, movement.scans, hall=movement.to_hall,
                          note="双端确定的跨馆移动")
            elif self.state == S_TRANSIT and self.origin == movement.from_hall:
                self._set(S_IN, movement.time, movement.scans, hall=movement.to_hall,
                          note="未证实的去向经双端记录落实")
            elif self.state in (S_UNKNOWN, S_OUTSIDE):
                self._set(S_IN, movement.time, movement.scans, hall=movement.to_hall,
                          note=f"进入 {movement.from_hall} 的过程无记录")
            elif self.state == S_IN:
                self._contest({movement.to_hall, self.hall}, movement.time, movement.scans,
                              "跨馆移动与当前在场馆相斥")
            else:
                self._contest({movement.to_hall, self.candidate}, movement.time, movement.scans,
                              "跨馆移动与未证实的去向相斥")
        elif kind == "corridor_exit_only":
            if self.state == S_IN and self.hall == movement.from_hall:
                self._set(S_TRANSIT, movement.time, movement.scans,
                          origin=movement.from_hall, candidate=movement.candidate_hall,
                          note="已离开原馆，目的地未证实")
            elif self.state in (S_UNKNOWN, S_OUTSIDE):
                self._set(S_TRANSIT, movement.time, movement.scans,
                          origin=movement.from_hall, candidate=movement.candidate_hall,
                          note=f"离开 {movement.from_hall} 前无在场记录")
            elif self.state == S_IN:
                self._contest({movement.candidate_hall, self.hall}, movement.time, movement.scans,
                              "连廊离场记录与当前在场馆相斥")
            else:
                self._contest({movement.candidate_hall, self.candidate}, movement.time, movement.scans,
                              "连廊离场记录与未证实的去向相斥")
        elif kind == "corridor_enter_only":
            hall = movement.to_hall
            if self.state == S_IN and self.hall == movement.candidate_hall:
                self._set(S_IN, movement.time, movement.scans, hall=hall,
                          note="来路由先前在场状态佐证")
            elif self.state == S_TRANSIT and self.candidate == hall:
                self._set(S_IN, movement.time, movement.scans, hall=hall,
                          note="去向经对端单记录落实")
            elif self.state in (S_UNKNOWN, S_OUTSIDE):
                self._set(S_IN, movement.time, movement.scans, hall=hall, note="来路未证实")
            elif self.state == S_IN:
                self._contest({hall, self.hall}, movement.time, movement.scans,
                              "连廊入馆记录与当前在场馆相斥")
            else:
                self._contest({hall, self.candidate}, movement.time, movement.scans,
                              "连廊入馆记录与未证实的去向相斥")


@dataclass
class ReplayResult:
    """一次按批次重放的全部事实。"""

    as_of: int
    identity: IdentityBook
    gates: dict[str, GateFact]
    scans: list[ScanFact]
    movements: list[Movement]
    rights: dict[RightKey, RightReplay]
    conflicts: list[ConflictFact]
    anonymous_movements: list[Movement]

    @property
    def pending(self) -> list[ScanFact]:
        return [s for s in self.scans if s.status == "pending"]

    @property
    def withdrawn(self) -> list[ScanFact]:
        return [s for s in self.scans if s.status == "withdrawn"]

    def scan_by_id(self, scan_id: str) -> ScanFact | None:
        for scan in self.scans:
            if scan.scan_id == scan_id:
                return scan
        return None

    def presence_at(self, moment, halls) -> PresenceSnapshot:
        certain: dict[str, list[RightKey]] = {h: [] for h in halls}
        possible: dict[str, list[RightKey]] = {h: [] for h in halls}
        for key, replay in self.rights.items():
            span = replay.state_at(moment)
            if span.state == S_IN:
                certain.setdefault(span.hall, []).append(key)
            elif span.state == S_TRANSIT:
                if span.candidate in possible:
                    possible[span.candidate].append(key)
            elif span.state == S_CONTESTED:
                for hall in span.candidates or ():
                    if hall in possible:
                        possible[hall].append(key)
        anonymous = {h: 0 for h in halls}
        for hall in halls:
            anonymous[hall] = self._anonymous_pool_at(hall, moment)
        return PresenceSnapshot(
            certain={h: tuple(v) for h, v in certain.items() if v},
            possible={h: tuple(v) for h, v in possible.items() if v},
            anonymous=anonymous,
        )

    def _anonymous_pool_at(self, hall: str, moment) -> int:
        """无令牌共用人次的净在场：只按人次计数，不拆到个人。"""
        pool = 0
        for movement in self.anonymous_movements:
            if movement.time > moment:
                continue
            if movement.kind == "outside_enter" and movement.to_hall == hall:
                pool += 1
            elif movement.kind == "outside_exit" and movement.from_hall == hall:
                pool -= 1
            elif movement.kind == "corridor_enter_only" and movement.to_hall == hall:
                pool += 1
            elif movement.kind == "corridor_exit_only" and movement.from_hall == hall:
                pool -= 1
            elif movement.kind == "corridor_confirmed":
                if movement.from_hall == hall:
                    pool -= 1
                if movement.to_hall == hall:
                    pool += 1
        return max(pool, 0)

    def rights_touching(self, hall: str, start, end) -> list[RightKey]:
        """值班区间内与该馆有关的通行权：有移动触及，或边界时刻状态涉及。"""
        touched = []
        for key, replay in self.rights.items():
            involved = any(
                hall in (m.from_hall, m.to_hall, m.candidate_hall)
                for m in replay.movements
                if start <= m.time <= end
            )
            if not involved:
                involved = replay.state_at(start).involves(hall) or replay.state_at(end).involves(hall)
            if involved:
                touched.append(key)
        return touched


def replay(sources: SourceBundle, as_of_batch: int | None = None) -> ReplayResult:
    """按到达批次重放全部通行事实；as_of_batch 为 None 时取最新批次。"""
    from .identity import build_identity

    batch = sources.latest_batch if as_of_batch is None else as_of_batch
    gates = _build_gates(sources)
    identity = build_identity(sources.tickets, batch)
    scans = _build_scans(sources, gates, identity, batch)
    movements, conflicts = _pair_movements(scans)

    rights: dict[RightKey, RightReplay] = {}
    anonymous: list[Movement] = []
    machines: dict[RightKey, _PresenceMachine] = {}
    for movement in movements:
        if movement.right is None:
            anonymous.append(movement)
            continue
        machine = machines.get(movement.right)
        if machine is None:
            machine = machines[movement.right] = _PresenceMachine(movement.right)
        machine.replay.movements.append(movement)
        machine.apply(movement)
    for key, machine in machines.items():
        rights[key] = machine.replay

    # 时间线相斥也登记为冲突事实
    for key, right_replay in rights.items():
        for span in right_replay.spans:
            if span.state == S_CONTESTED:
                conflicts.append(
                    ConflictFact(
                        kind="时间线相斥",
                        scans=tuple(span.basis),
                        right=key,
                        detail=span.note,
                    )
                )

    return ReplayResult(
        as_of=batch,
        identity=identity,
        gates=gates,
        scans=scans,
        movements=movements,
        rights=rights,
        conflicts=conflicts,
        anonymous_movements=anonymous,
    )
