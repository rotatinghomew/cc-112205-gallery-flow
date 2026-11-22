"""人流事实仓命令行。

用法（项目根目录）：
    python3 -m flowfact build [--as-of N]      各班次交班结论
    python3 -m flowfact replay [--as-of N]     各通行权在场时间线
    python3 -m flowfact movements [--as-of N]  移动事实（含单端与相斥）
    python3 -m flowfact scans [--as-of N]      闸机事实（含撤销与待核）
    python3 -m flowfact sign SHIFT [--as-of N] 签收班次交班结论
    python3 -m flowfact signatures             查看已签记录
    python3 -m flowfact recheck [--as-of N]    重新核查，提示受影响班次
    python3 -m flowfact explain HALL           从结论解释到原始凭据
"""
from __future__ import annotations

import argparse
import sys

from .handover import VERDICT_LABELS
from .replay import S_CONTESTED, S_IN, S_OUTSIDE, S_TRANSIT, S_UNKNOWN, describe_movement
from .warehouse import FactWarehouse

_STATE_LABELS = {
    S_UNKNOWN: "无记录",
    S_OUTSIDE: "馆外",
    S_IN: "在馆",
    S_TRANSIT: "去向未证实",
    S_CONTESTED: "记录相斥",
}


def _fmt(moment) -> str:
    return moment.strftime("%m-%d %H:%M") if moment else "--"


def _add_common(parser: argparse.ArgumentParser):
    parser.add_argument("--data", default="data", help="资料目录（默认 data）")
    parser.add_argument("--store", default="warehouse.db", help="签收记录库（默认 warehouse.db）")
    parser.add_argument("--as-of", type=int, default=None, dest="as_of", help="按到达批次重放")


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)
    parser = argparse.ArgumentParser(prog="flowfact", description="联展门区人流事实仓")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("build", "replay", "movements", "scans", "signatures", "recheck"):
        sub.add_parser(name, parents=[common])
    sign_parser = sub.add_parser("sign", parents=[common])
    sign_parser.add_argument("shift")
    explain_parser = sub.add_parser("explain", parents=[common])
    explain_parser.add_argument("hall")
    args = parser.parse_args(argv)

    warehouse = FactWarehouse(data_dir=args.data, store_path=args.store)
    as_of = args.as_of

    if args.cmd == "build":
        verdicts = warehouse.verdicts(as_of)
        batch = warehouse.sources.latest_batch if as_of is None else as_of
        print(f"到达批次 ≤ {batch} 的交班结论：")
        for verdict in verdicts:
            print(f"  {verdict.shift}（{verdict.hall} 馆，运营日 {verdict.operating_day}）：{verdict.label}")
            for reason in verdict.reasons:
                print(f"    - {reason}")
        if not verdicts:
            print("  （该批次下还没有班次资料）")
    elif args.cmd == "replay":
        result = warehouse.replay(as_of)
        print(f"到达批次 ≤ {result.as_of} 的通行权在场时间线：")
        for key, right in sorted(result.rights.items(), key=lambda item: item[0].label):
            tokens = "、".join(t.token for t in result.identity.rights[key].tickets)
            print(f"  通行权 {key.label}（令牌 {tokens}）：")
            for span in right.spans:
                where = span.hall or span.candidate or ""
                print(f"    {_fmt(span.since)} 起 {_STATE_LABELS[span.state]} {where} {span.note}".rstrip())
    elif args.cmd == "movements":
        result = warehouse.replay(as_of)
        for movement in result.movements:
            who = movement.right.label if movement.right else "（无令牌人次）"
            print(
                f"  {_fmt(movement.time)} {movement.kind} {who} "
                f"{describe_movement(movement)} 凭 {'、'.join(movement.scans)} {movement.note}".rstrip()
            )
        for conflict in result.conflicts:
            print(f"  相斥[{conflict.kind}] {'、'.join(conflict.scans)}：{conflict.detail}")
    elif args.cmd == "scans":
        result = warehouse.replay(as_of)
        for scan in result.scans:
            gate = scan.gate.gate if scan.gate else scan.raw.get("gate")
            extra = f"（{'；'.join(scan.notes)}）" if scan.notes else ""
            print(
                f"  {scan.scan_id} [{scan.status}] {gate} {scan.direction} "
                f"{scan.ticket_key}/{scan.token} @{_fmt(scan.time)} 批次{scan.deliveries}{extra}"
            )
    elif args.cmd == "sign":
        try:
            record = warehouse.sign(args.shift, as_of)
        except KeyError as exc:
            print(exc.args[0], file=sys.stderr)
            return 1
        print(f"已签收 {record['signature_id']}：{record['shift']}（{record['hall']} 馆）"
              f"按批次 {record['as_of']} 结论 {VERDICT_LABELS[record['payload']['verdict']]}")
    elif args.cmd == "signatures":
        for record in warehouse.signatures():
            print(
                f"  {record['signature_id']}：{record['shift']}（{record['hall']} 馆）"
                f"按批次 {record['as_of']} 签 {VERDICT_LABELS[record['payload']['verdict']]}"
            )
    elif args.cmd == "recheck":
        report = warehouse.recheck(as_of)
        print(f"按批次 {report.as_of} 重新核查：")
        for entry in report.entries:
            mark = "受影响" if entry.changed else "维持"
            current = VERDICT_LABELS.get(entry.current_verdict, "无班次")
            print(
                f"  {entry.signature_id}（{entry.hall} 馆 {entry.shift}）："
                f"签收 {VERDICT_LABELS.get(entry.signed_verdict)} → 现 {current}，{mark}。{entry.note}"
            )
        if report.unsigned_shifts:
            print(f"  未签收班次：{'、'.join(report.unsigned_shifts)}")
        if report.touched_halls:
            print(f"  签收后新资料直接触及的场馆：{'、'.join(report.touched_halls)}")
    elif args.cmd == "explain":
        print(warehouse.explain(args.hall, as_of))
    return 0


if __name__ == "__main__":
    sys.exit(main())
