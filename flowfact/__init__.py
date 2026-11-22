"""联展门区人流事实仓。

把票务、闸机、撤销单、值班、覆盖、清点等交付资料整理成运营要用的
人流事实：按到达批次重放实际通行，校核各馆各班次的可交班范围，
已签结论可按当时资料查回，新资料到达后提示受影响的场馆和班次。
"""

from .handover import INSUFFICIENT, OCCUPIED, READY, TENTATIVE, ShiftVerdict, verify_all
from .identity import RightKey, build_identity
from .replay import ReplayResult, replay
from .sources import SourceBundle, load_sources
from .warehouse import FactWarehouse

__all__ = [
    "FactWarehouse",
    "INSUFFICIENT",
    "OCCUPIED",
    "READY",
    "ReplayResult",
    "ShiftVerdict",
    "SourceBundle",
    "TENTATIVE",
    "RightKey",
    "build_identity",
    "load_sources",
    "replay",
    "verify_all",
]
