"""凭据身份与通行权：渠道+账号定义凭据，令牌拆人，换票接续在场。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowfact.identity import build_identity
from flowfact.sources import load_sources

DATA = Path(__file__).resolve().parents[1] / "data"


def ticket_row(key, channel="onsite", account="U9", code="17", token="p1",
               allowed="A,B", carry_from="", received="1"):
    return {
        "key": key, "channel": channel, "account": account, "code": code,
        "token": token, "allowed": allowed,
        "from": "2027-03-10T18:00+08:00", "to": "2027-03-11T03:00+08:00",
        "carry_from": carry_from, "received": received,
    }


class IdentityTests(unittest.TestCase):
    def test_carry_chain_joins_exchange_ticket_into_same_right(self):
        identity = build_identity(load_sources(DATA).tickets, as_of=8)
        # T04 经 carry_from=T01/p1 接续 T01 的通行权
        self.assertEqual(identity.ticket_right["T04"], identity.ticket_right["T01"])
        root = identity.ticket_right["T01"]
        self.assertEqual(root.root_token, "p1")
        self.assertEqual(root.channel, "onsite")
        self.assertEqual(root.account, "U9")
        # 同一通行权上的令牌链
        self.assertEqual(identity.rights[root].tokens, ("p1", "p7"))

    def test_same_credential_split_by_token(self):
        identity = build_identity(load_sources(DATA).tickets, as_of=8)
        # T01 与 T03 同渠道同账号同票种（凭据相同），令牌不同 => 不同人在场
        self.assertNotEqual(identity.ticket_right["T01"], identity.ticket_right["T03"])
        # T02 渠道不同，是另一个凭据作用域
        self.assertNotEqual(identity.ticket_right["T02"], identity.ticket_right["T01"])

    def test_carry_from_not_yet_arrived_is_noted(self):
        rows = [ticket_row("T09", token="p9", carry_from="T99/p1", received="2")]
        identity = build_identity(rows, as_of=2)
        self.assertTrue(any("T99" in issue for issue in identity.issues))
        # 交接号指向未到的票时，先按独立通行权处理
        self.assertEqual(identity.ticket_right["T09"].root_token, "p9")

    def test_as_of_filters_future_tickets(self):
        sources = load_sources(DATA)
        early = build_identity(sources.tickets, as_of=2)
        self.assertNotIn("T04", early.tickets)  # T04 第 3 批才到
        self.assertIn("T01", early.tickets)


if __name__ == "__main__":
    unittest.main()
