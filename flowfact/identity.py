"""入场凭据与通行权。

- 凭据身份由渠道和出票账号定义（channel + account）。
- 同一凭据可由多人次共用；只有入场时登记的独立通行令牌（token）
  能把在场拆到不同的人。无令牌的团体同行记录只形成人次事实，
  不能据此补出个人移动。
- 换票交接号 ``carry_from`` 明确指向同一通行权时（票键/令牌），
  新令牌接续已有的在场状态，两条令牌属于同一通行权。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .sources import batch_of, parse_time


@dataclass(frozen=True)
class RightKey:
    """通行权键：凭据作用域内的根令牌。"""

    channel: str
    account: str
    root_token: str

    @property
    def label(self) -> str:
        return f"{self.channel}/{self.account}#{self.root_token}"


@dataclass(frozen=True)
class TicketFact:
    """票务清册中的一行（含换票关系）。"""

    key: str
    channel: str
    account: str
    code: str
    token: str
    allowed: frozenset[str]
    valid_from: object
    valid_to: object
    carry_from: tuple[str, str] | None
    batch: int
    raw: dict

    @property
    def credential(self) -> str:
        return f"{self.channel}/{self.account}"


@dataclass
class PassageRight:
    """一条通行权：同一访客在场状态的承接链。"""

    key: RightKey
    tickets: list[TicketFact] = field(default_factory=list)

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(ticket.token for ticket in self.tickets)

    @property
    def allowed(self) -> frozenset[str]:
        halls: set[str] = set()
        for ticket in self.tickets:
            halls |= set(ticket.allowed)
        return frozenset(halls)

    @property
    def label(self) -> str:
        return self.key.label


@dataclass(frozen=True)
class IdentityBook:
    """按批次重建出的身份台账。"""

    tickets: dict[str, TicketFact]
    rights: dict[RightKey, PassageRight]
    ticket_right: dict[str, RightKey]
    issues: tuple[str, ...]


def _parse_carry(text: str) -> tuple[str, str] | None:
    """carry_from 形如 'T01/p1'，指向同一通行权的上一张票。"""
    text = (text or "").strip()
    if not text:
        return None
    key, _, token = text.partition("/")
    return key.strip(), token.strip()


def build_identity(ticket_rows, as_of: int) -> IdentityBook:
    """按到达批次重建凭据与通行权。

    换票链用并查思路：carry_from 指向的票与当前票属于同一通行权，
    根令牌取链上最先生效（无 carry_from）那张票的令牌。
    """
    tickets: dict[str, TicketFact] = {}
    issues: list[str] = []
    for row in sorted(ticket_rows, key=lambda r: (batch_of(r), r["key"])):
        if batch_of(row) > as_of:
            continue
        tickets[row["key"]] = TicketFact(
            key=row["key"],
            channel=row["channel"],
            account=row["account"],
            code=row["code"],
            token=row["token"],
            allowed=frozenset(h.strip() for h in row["allowed"].split(",") if h.strip()),
            valid_from=parse_time(row["from"]),
            valid_to=parse_time(row["to"]),
            carry_from=_parse_carry(row.get("carry_from", "")),
            batch=batch_of(row),
            raw=row,
        )

    # 无令牌的票不建立个人通行权：共用人次无法拆到个人
    person_tickets = {key: t for key, t in tickets.items() if t.token}

    # 并查集：把换票链上的票键连起来
    parent = {key: key for key in person_tickets}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for ticket in person_tickets.values():
        if ticket.carry_from is None:
            continue
        target_key, target_token = ticket.carry_from
        target = person_tickets.get(target_key)
        if target is None:
            issues.append(f"{ticket.key} 的换票交接号指向未到的票 {target_key}")
            continue
        if target.token != target_token:
            issues.append(f"{ticket.key} 的换票交接号令牌不符：{target_key}/{target_token}")
            continue
        parent[find(ticket.key)] = find(target.key)

    # 根令牌：链上无 carry_from 的票；若都有（异常环），取排序最小者兜底
    groups: dict[str, list[TicketFact]] = {}
    for ticket in person_tickets.values():
        groups.setdefault(find(ticket.key), []).append(ticket)

    rights: dict[RightKey, PassageRight] = {}
    ticket_right: dict[str, RightKey] = {}
    for members in groups.values():
        members.sort(key=lambda t: (t.valid_from, t.key))
        roots = [t for t in members if t.carry_from is None]
        root = roots[0] if roots else members[0]
        key = RightKey(channel=root.channel, account=root.account, root_token=root.token)
        right = PassageRight(key=key, tickets=members)
        rights[key] = right
        for ticket in members:
            ticket_right[ticket.key] = key

    return IdentityBook(
        tickets=tickets,
        rights=rights,
        ticket_right=ticket_right,
        issues=tuple(issues),
    )
