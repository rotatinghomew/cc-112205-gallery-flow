import csv
import json
from pathlib import Path
import sqlite3
import unittest


DATA = Path(__file__).resolve().parents[1] / "data"


def load(name):
    """读取资料方原格式，保留原始字段。"""
    path = DATA / name
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as source:
            return list(csv.DictReader(source))
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if path.suffix == ".db":
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            return [dict(row) for row in source.execute(f'SELECT * FROM "{path.stem}"')]
    if path.suffix == ".parquet":
        import duckdb

        with duckdb.connect() as source:
            result = source.execute("SELECT * FROM read_parquet(?)", [str(path)])
            names = [column[0] for column in result.description]
            return [dict(zip(names, row)) for row in result.fetchall()]
    raise ValueError(f"未知的资料格式 {path.suffix}")


class SourceContractTests(unittest.TestCase):
    def test_source_examples_are_present(self):
        checks = [('scans.jsonl', 'pass', 'P13', 4), ('tickets.csv', 'code', '17', 3), ('coverage.db', 'gate', 'BC', 2)]
        for name, field, value, count in checks:
            with self.subTest(source=name, field=field):
                self.assertEqual(sum(row[field] == value for row in load(name)), count)

    def test_passage_gates_match_issuance_and_scan_evidence(self):
        from datetime import datetime

        tickets = {row["key"]: row for row in load("tickets.csv")}
        gates = {row["gate"]: row for row in load("gates.csv")}
        scans = load("scans.jsonl")
        for scan in scans:
            self.assertIn(scan["gate"], gates)
            self.assertEqual(tickets[scan["ticket"]]["token"], scan["token"])
            self.assertIsNotNone(datetime.fromisoformat(scan["time"]).utcoffset())
        for certificate in load("withdrawals.jsonl"):
            self.assertTrue(any(row["scan"] == certificate["scan"] and row["gate"] == certificate["issuer"] for row in scans))
        self.assertTrue(all(row["gate"] in gates for row in load("coverage.db")))
