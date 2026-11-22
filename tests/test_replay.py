"""重放实际通行：移动配对、撤销、相斥、在场时间线、跨午夜。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowfact.replay import S_CONTESTED, S_IN, S_OUTSIDE, S_TRANSIT, replay
from flowfact.sources import SourceBundle, load_sources, parse_time

DATA = Path(__file__).resolve().parents[1] / "data"
SOURCES = load_sources(DATA)

P1 = "onsite/U9#p1"   # T01/p1 换票接续 T04/p7
P2 = "onsite/U9#p2"   # T03
P3 = "partner/U9#p3"  # T02


def right(result, label):
    for key, replay_ in result.rights.items():
        if key.label == label:
            return replay_
    raise KeyError(label)


def at(result, label, moment):
    return right(result, label).state_at(parse_time(moment))


class MovementPairingTests(unittest.TestCase):
    def test_conflicting_pass_ends_stay_one_end_at_batch5(self):
        result = replay(SOURCES, 5)
        kinds = {m.kind for m in result.movements}
        self.assertIn("corridor_exit_only", kinds)
        self.assertIn("corridor_enter_only", kinds)
        self.assertNotIn("corridor_confirmed", kinds)
        # 同一通行号 P13 两端身份不符，登记相斥而不是擅自配对
        conflicts = [c for c in result.conflicts if c.kind == "两端身份相斥"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(set(conflicts[0].scans), {"S03", "S04"})

    def test_late_second_end_completes_confirmed_movement(self):
        result = replay(SOURCES, 7)
        confirmed = [m for m in result.movements if m.kind == "corridor_confirmed"]
        self.assertEqual(len(confirmed), 1)
        move = confirmed[0]
        self.assertEqual(move.from_hall, "A")
        self.assertEqual(move.to_hall, "B")
        self.assertEqual(set(move.scans), {"S03", "S05"})
        self.assertEqual(move.pass_id, "P13")
        self.assertEqual(move.right.label, P1)


class WithdrawalTests(unittest.TestCase):
    def test_withdrawal_removes_scan_from_movements(self):
        result = replay(SOURCES, 6)
        self.assertIn("S04", {s.scan_id for s in result.withdrawn})
        self.assertNotIn("S04", {sid for m in result.movements for sid in m.scans})
        # 撤销后 p2 的时间线恢复干净：进 A 后从 A 离场
        span = at(result, P2, "2027-03-11T01:00+08:00")
        self.assertEqual(span.state, S_OUTSIDE)

    def test_redelivered_scan_id_stays_withdrawn(self):
        # S04 第 8 批同号补报，但第 6 批的正式撤销仍然有效
        result = replay(SOURCES, 8)
        scan = result.scan_by_id("S04")
        self.assertEqual(scan.status, "withdrawn")
        self.assertEqual(scan.deliveries, (4, 8))
        self.assertNotIn("S04", {sid for m in result.movements for sid in m.scans})


class PresenceTimelineTests(unittest.TestCase):
    def test_exchange_ticket_continues_presence(self):
        result = replay(SOURCES, 8)
        # p1 入场、换票为 p7 后在场馆间移动，在场状态始终是同一条通行权
        self.assertEqual(at(result, P1, "2027-03-10T22:30+08:00").state, S_IN)
        self.assertEqual(at(result, P1, "2027-03-10T22:30+08:00").hall, "A")
        self.assertEqual(at(result, P1, "2027-03-11T00:50+08:00").hall, "B")
        self.assertEqual(at(result, P1, "2027-03-11T01:30+08:00").state, S_OUTSIDE)

    def test_midnight_does_not_clear_presence(self):
        result = replay(SOURCES, 8)
        # 跨日连排：午夜仍在场的人保留，不到零点一律清空
        for moment in ("2027-03-11T00:00+08:00", "2027-03-11T00:30+08:00"):
            self.assertEqual(at(result, P1, moment).hall, "A")
            self.assertEqual(at(result, P2, moment).hall, "A")

    def test_one_end_exit_keeps_left_and_destination_unconfirmed(self):
        result = replay(SOURCES, 6)  # S05 未到，S03 只有本端
        span = at(result, P1, "2027-03-11T01:00+08:00")
        self.assertEqual(span.state, S_TRANSIT)
        self.assertEqual(span.origin, "A")       # 已离开原馆
        self.assertEqual(span.candidate, "B")    # 目的地未证实

    def test_conflicting_records_keep_candidates(self):
        result = replay(SOURCES, 5)  # S04 未撤销：p2 既报进 B 又报从 A 离场
        span = at(result, P2, "2027-03-11T01:00+08:00")
        self.assertEqual(span.state, S_CONTESTED)
        self.assertEqual(span.candidates, frozenset({None, "B"}))  # 馆外或 B

    def test_presence_snapshot_at_handover(self):
        result = replay(SOURCES, 7)
        snapshot = result.presence_at(parse_time("2027-03-11T01:00+08:00"), ["A", "B", "C"])
        self.assertEqual([k.label for k in snapshot.certain_in("B")], [P1])
        self.assertEqual(snapshot.certain_in("A"), ())
        self.assertEqual(snapshot.certain_in("C"), ())


class PendingAndAnonymousTests(unittest.TestCase):
    def _bundle(self, extra_scans=(), extra_tickets=()):
        return SourceBundle(
            tickets=SOURCES.tickets + tuple(extra_tickets),
            gates=SOURCES.gates,
            scans=SOURCES.scans + tuple(extra_scans),
            withdrawals=SOURCES.withdrawals,
            shifts=SOURCES.shifts,
            coverage=SOURCES.coverage,
            headcounts=SOURCES.headcounts,
        )

    def test_illegal_scan_is_kept_pending_not_erased(self):
        bad = {
            "scan": "S91", "gate": "AO", "direction": "enter", "ticket": "T01",
            "token": "p1", "pass": "", "time": "2027-03-11T04:00+08:00", "received": "8",
        }  # 已超出票务有效期
        result = replay(self._bundle(extra_scans=[bad]), 8)
        scan = result.scan_by_id("S91")
        self.assertEqual(scan.status, "pending")
        self.assertTrue(any("有效期" in note for note in scan.notes))
        self.assertNotIn("S91", {sid for m in result.movements for sid in m.scans})

    def test_tokenless_group_scans_form_anonymous_pool_not_persons(self):
        ticket = {
            "key": "T50", "channel": "onsite", "account": "U9", "code": "30",
            "token": "", "allowed": "A", "from": "2027-03-10T18:00+08:00",
            "to": "2027-03-11T03:00+08:00", "carry_from": "", "received": "1",
        }
        scans = [
            {"scan": "S81", "gate": "AO", "direction": "enter", "ticket": "T50",
             "token": "", "pass": "", "time": "2027-03-10T21:00+08:00", "received": "2"},
            {"scan": "S82", "gate": "AO", "direction": "enter", "ticket": "T50",
             "token": "", "pass": "", "time": "2027-03-10T21:05+08:00", "received": "2"},
            {"scan": "S83", "gate": "AO", "direction": "exit", "ticket": "T50",
             "token": "", "pass": "", "time": "2027-03-10T22:30+08:00", "received": "2"},
        ]
        bundle = self._bundle(extra_scans=scans, extra_tickets=[ticket])
        result = replay(bundle, 8)
        # 无令牌的团体同行记录只形成人次，不拆出个人通行权
        self.assertNotIn("T50", result.identity.ticket_right)
        snapshot = result.presence_at(parse_time("2027-03-11T01:00+08:00"), ["A"])
        self.assertEqual(snapshot.anonymous["A"], 1)  # 进 2 出 1，净 1 人次
        # 也不得凭同凭据的令牌持有人补齐
        self.assertNotIn(P1, [k.label for k in snapshot.certain_in("A")])


if __name__ == "__main__":
    unittest.main()
