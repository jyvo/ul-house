"""scope rules over list rows where applied"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from ul_house.crawl.discover import ListRow
from ul_house.crawl.frontier import Entry
from ul_house.settings import Scope


@dataclass(frozen=True)
class Decision:
    keep: bool
    reason: str


def decide(row: ListRow, scope: Scope) -> Decision:
    if scope.names.excluded_token(row.name):
        return Decision(False, "excluded_name")
    if row.cost == scope.cost.cosmetic:
        return Decision(False, "cosmetic")
    if scope.names.bypass_token(row.name):
        return Decision(True, "name_token")
    if row.rarity not in scope.rarity.include:
        return Decision(False, "rarity")
    if scope.cost.contains(row.cost):
        return Decision(True, "cost_band")
    return Decision(False, "cost")


@dataclass(frozen=True)
class Selection:
    kept: tuple[tuple[ListRow, Decision], ...]
    reasons: Counter

    def entries(self) -> list[Entry]:
        """depth 0 entries: listed, catalog, keep rule/reason"""
        return [Entry(row.item_id, "list", "catalog", decision.reason) for row, decision in self.kept]


def select(rows: Iterable[ListRow], scope: Scope) -> Selection:
    kept, reasons = [], Counter()
    for row in rows:
        decision = decide(row, scope)
        reasons[decision.reason] += 1
        if decision.keep:
            kept.append((row, decision))
    kept.sort(key=lambda pair: pair[0].item_id)
    return Selection(tuple(kept), reasons)
