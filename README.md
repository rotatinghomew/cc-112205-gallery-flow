# 联展门区交班资料

连通的展厅按各自值班区间交接，票务、闸机和点数记录均为合成记录。

## 资料格式

CSV 和 JSON Lines 均为 UTF-8。SQLite 来源库只读，原始标识保持文本类型。同时交付的多行可以有相同的 `received`，这个字段表示到达批次，不用行号决定同批事实的优先级。

`tickets` 发布合法场馆和有效期及换票关系；`account` 和 `channel` 是出票作用域。扫码令牌由票务在入馆时分配和登记，只有明确承接的令牌共享状态，无令牌的团体同行记录不能补出个人移动。`pass` 非空时由连廊双端共用，`link` 是场馆公布的连通关系。只有身份、方向和拓扑都相符的两端能当作一笔确定移动；单端只能证明自己的出/入，已过边界的访客可同时保留「已离开原馆／目的地未证实」状态。不合法的扫票保留待核，不能抹去闸机报告和实际占用可能性。
日期时间均带 +08:00，运营日的起点及闭馆交班时间见 `shifts`。`coverage` 指值班确认门区资料完备的区间，包含开头而不含终点。`headcounts` 是无个人明细的实际点数，仅能证实或质疑该时刻的总体在场。已签单引用当时的资料可见次序。

- `tickets.csv`。票务有效期与换票清册。`allowed` 含各馆短码，`carry_from` 为空时是独立凭据。 字段为 `key`、`channel`、`account`、`code`、`token`、`allowed`、`from`、`to`、`carry_from`、`received`。
- `gates.csv`。闸机及相连门区，`outside` 是馆外，不形成对馆内另一闸机的要求。 字段为 `gate`、`hall`、`link`、`other`。
- `scans.jsonl`。闸机事实按取得时刻登记，`received` 是汇入顺序。 字段为 `scan`、`gate`、`direction`、`ticket`、`token`、`pass`、`time`、`received`。
- `withdrawals.jsonl`。闸机方的正式撤销单，避免把后到的同号扫描直接当有效。 字段为 `scan`、`issuer`、`reason`、`received`。
- `shifts.csv`。值班区间及已确认的交班凭据。 字段为 `shift`、`hall`、`from`、`handover`、`previous`、`received`。
- `coverage.db`。各门区可回查的资料覆盖记录。 字段为 `gate`、`from`、`to`、`received`。 SQLite 表名为 `coverage`。
- `headcounts.csv`。领班现场点数，无法据此识别与闸机不同的是哪位访客。 字段为 `hall`、`time`、`count`、`received`。

## 核对交付资料

使用 Python 3.11 和标准库。
在项目根目录运行下列命令，核对原始来源键、实际文件和格式。

```sh
python3 -m unittest discover -s tests -v
```
