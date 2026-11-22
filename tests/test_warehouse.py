"""人流事实仓：签收可查回、修正提示影响、远处场馆无需撤签、解释到原始凭据。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowfact.warehouse import FactWarehouse

DATA = Path(__file__).resolve().parents[1] / "data"


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "warehouse.db"
        self.warehouse = FactWarehouse(data_dir=DATA, store_path=self.store)

    def _sign_all_at_batch5(self):
        for shift in ("H-A", "H-B", "H-C"):
            self.warehouse.sign(shift, as_of_batch=5)

    def test_signed_record_queryable_as_of_signing_batch(self):
        self._sign_all_at_batch5()
        records = {r["shift"]: r for r in self.warehouse.signatures()}
        # 已签单引用当时的资料可见次序
        self.assertEqual(records["H-A"]["as_of"], 5)
        self.assertEqual(records["H-A"]["payload"]["verdict"], "INSUFFICIENT")
        self.assertEqual(records["H-C"]["payload"]["verdict"], "READY")
        # 按当时批次重放，结论可复现（查回）
        report = self.warehouse.recheck(as_of_batch=5)
        self.assertTrue(all(entry.integrity_ok for entry in report.entries))
        self.assertFalse(any(entry.changed for entry in report.entries))

    def test_recheck_flags_affected_and_spares_distant_halls(self):
        self._sign_all_at_batch5()
        report = self.warehouse.recheck()  # 最新批次 8
        entries = {e.shift: e for e in report.entries}
        # 晚到记录使甲馆转为可交班、乙馆因仍有人不能交班
        self.assertTrue(entries["H-A"].changed)
        self.assertEqual(entries["H-A"].current_verdict, "READY")
        self.assertTrue(entries["H-B"].changed)
        self.assertEqual(entries["H-B"].current_verdict, "OCCUPIED")
        # 新资料未涉及的远处场馆无需撤签
        self.assertFalse(entries["H-C"].changed)
        self.assertIn("无需撤签", entries["H-C"].note)
        # 影响提示：受波及的场馆与班次
        self.assertEqual(report.touched_halls, ("A", "B"))
        self.assertEqual({e.shift for e in report.affected}, {"H-A", "H-B"})

    def test_unsigned_shifts_are_listed(self):
        self.warehouse.sign("H-C", as_of_batch=5)
        report = self.warehouse.recheck()
        self.assertEqual(set(report.unsigned_shifts), {"H-A", "H-B"})

    def test_explain_traces_verdict_to_raw_gate_records(self):
        text = self.warehouse.explain("B", as_of_batch=7)
        # 总体结论
        self.assertIn("不能交班", text)
        self.assertIn("onsite/U9#p1", text)
        # 直到闸机原始凭据
        self.assertIn('"scan": "S05"', text)
        self.assertIn('"scan": "S03"', text)
        self.assertIn('"pass": "P13"', text)
        # 撤销单及其原因
        self.assertIn("S04", text)
        self.assertIn("设备缓存", text)

    def test_recheck_after_gate_supplement_explainable(self):
        # 收到同一门（BC）的补数后，受影响时段可重新核查
        self.warehouse.sign("H-B", as_of_batch=5)
        report = self.warehouse.recheck(as_of_batch=7)  # BC 补数到达
        entry = report.entries[0]
        self.assertTrue(entry.changed)
        self.assertEqual(entry.signed_verdict, "INSUFFICIENT")
        self.assertEqual(entry.current_verdict, "OCCUPIED")
        # 签收时的结论仍可按当时资料复现
        self.assertTrue(entry.integrity_ok)


if __name__ == "__main__":
    unittest.main()
