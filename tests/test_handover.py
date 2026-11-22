"""校核可交班范围：四档结论、覆盖缺口、清点佐证与冲突。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowfact.handover import INSUFFICIENT, OCCUPIED, READY, TENTATIVE, verify_all
from flowfact.replay import replay
from flowfact.sources import SourceBundle, load_sources

DATA = Path(__file__).resolve().parents[1] / "data"
SOURCES = load_sources(DATA)


def verdicts_at(batch):
    result = replay(SOURCES, batch)
    return {v.shift: v for v in verify_all(SOURCES, result)}


class VerdictByBatchTests(unittest.TestCase):
    def test_batch5_both_corridor_halls_lack_evidence(self):
        verdicts = verdicts_at(5)
        self.assertEqual(verdicts["H-A"].verdict, INSUFFICIENT)
        self.assertEqual(verdicts["H-B"].verdict, INSUFFICIENT)
        self.assertEqual(verdicts["H-C"].verdict, READY)
        a_reasons = "；".join(verdicts["H-A"].reasons)
        self.assertIn("onsite/U9#p1", a_reasons)   # 去向未证实
        self.assertIn("onsite/U9#p2", a_reasons)   # 记录相斥
        b_reasons = "；".join(verdicts["H-B"].reasons)
        self.assertIn("BC", b_reasons)             # 缺门区证据

    def test_batch6_withdrawal_clears_p2_but_not_transit(self):
        verdicts = verdicts_at(6)
        a_reasons = "；".join(verdicts["H-A"].reasons)
        self.assertIn("onsite/U9#p1", a_reasons)
        self.assertNotIn("onsite/U9#p2", a_reasons)  # 撤销后 p2 时间线已干净
        self.assertEqual(verdicts["H-A"].verdict, INSUFFICIENT)

    def test_batch7_late_record_flips_both_halls(self):
        # 一笔晚到的跨馆记录：甲馆转为可交班，乙馆因仍有人而不能交班
        verdicts = verdicts_at(7)
        self.assertEqual(verdicts["H-A"].verdict, READY)
        self.assertEqual(verdicts["H-B"].verdict, OCCUPIED)
        self.assertEqual([k.label for k in verdicts["H-B"].certain], ["onsite/U9#p1"])
        self.assertEqual(verdicts["H-B"].headcount.status, "corroborated")
        self.assertEqual(verdicts["H-B"].headcount.reported, 1)

    def test_batch8_late_exit_after_handover_keeps_verdict(self):
        # S07 在交班时刻之后，不改变 01:00 的在场结论
        verdicts = verdicts_at(8)
        self.assertEqual(verdicts["H-B"].verdict, OCCUPIED)
        self.assertEqual(verdicts["H-A"].verdict, READY)

    def test_operating_day_and_cross_midnight_range(self):
        verdicts = verdicts_at(8)
        self.assertEqual(verdicts["H-A"].operating_day, "2027-03-10")
        self.assertEqual(verdicts["H-A"].start.hour, 20)
        self.assertEqual(verdicts["H-A"].handover.hour, 1)  # 次日 01:00，未按零点截断


class HeadcountTests(unittest.TestCase):
    def _with_headcount(self, count, batch="5"):
        rows = [r for r in SOURCES.headcounts if r["hall"] != "C"]
        rows.append({"hall": "C", "time": "2027-03-11T01:00+08:00",
                     "count": str(count), "received": batch})
        return SourceBundle(
            tickets=SOURCES.tickets, gates=SOURCES.gates, scans=SOURCES.scans,
            withdrawals=SOURCES.withdrawals, shifts=SOURCES.shifts,
            coverage=SOURCES.coverage,
            headcounts=tuple(rows),
        )

    def test_conflicting_headcount_makes_tentative_without_averaging(self):
        sources = self._with_headcount(2)  # 闸机已核 0，清点报 2
        result = replay(sources, 5)
        verdict = {v.shift: v for v in verify_all(sources, result)}["H-C"]
        self.assertEqual(verdict.verdict, TENTATIVE)
        self.assertEqual(verdict.headcount.status, "conflict")
        self.assertEqual(verdict.headcount.reported, 2)
        self.assertEqual(verdict.headcount.derived, 0)  # 差额不得均摊到人次
        self.assertIn("不得均摊", verdict.headcount.note)

    def test_missing_headcount_makes_tentative(self):
        rows = tuple(r for r in SOURCES.headcounts if r["hall"] != "C")
        sources = SourceBundle(
            tickets=SOURCES.tickets, gates=SOURCES.gates, scans=SOURCES.scans,
            withdrawals=SOURCES.withdrawals, shifts=SOURCES.shifts,
            coverage=SOURCES.coverage, headcounts=rows,
        )
        result = replay(sources, 5)
        verdict = {v.shift: v for v in verify_all(sources, result)}["H-C"]
        self.assertEqual(verdict.verdict, TENTATIVE)
        self.assertEqual(verdict.headcount.status, "missing")


class CoverageTests(unittest.TestCase):
    def test_gap_intervals_are_exact(self):
        verdicts = verdicts_at(5)
        bc = next(c for c in verdicts["H-B"].coverage if c.gate == "BC")
        self.assertEqual(len(bc.gaps), 1)
        lo, hi = bc.gaps[0]
        self.assertEqual((lo.hour, lo.minute), (0, 15))
        self.assertEqual((hi.hour, hi.minute), (1, 0))

    def test_late_coverage_closes_gap(self):
        verdicts = verdicts_at(7)
        bc = next(c for c in verdicts["H-B"].coverage if c.gate == "BC")
        self.assertTrue(bc.complete)


if __name__ == "__main__":
    unittest.main()
