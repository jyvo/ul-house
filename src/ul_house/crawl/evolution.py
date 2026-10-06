from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ul_house.crawl.discover import ListRow
from ul_house.crawl.frontier import Entry, Frontier
from ul_house.select import decide
from ul_house.seed.store import Store
from ul_house.settings import POLICIES, Scope


@dataclass(frozen=True, order=True)
class Hop:
    target_id: str
    kind: str
    parent_id: str
    target_name: str


def hops_from(store: Store, parent_ids: list[str], kinds: set[str], direction: str) -> list[Hop]:
    hops = set()
    if direction in ("predecessors", "both"):
        for link in store.links_from(parent_ids):
            if link["side"] == "before" and link["kind"] in kinds:
                hops.add(Hop(link["target_id"], link["kind"], link["source_id"], link["target_name"]))
    if direction in ("successors", "both"):
        for link in store.links_from(parent_ids):
            if link["side"] == "after" and link["kind"] in kinds:
                hops.add(Hop(link["target_id"], link["kind"], link["source_id"], link["target_name"]))
        for link in store.links_to(parent_ids):
            if link["side"] == "before" and link["kind"] in kinds:
                hops.add(Hop(link["source_id"], link["kind"], link["target_id"], ""))
    return sorted(hops)


def _weaker(a: str, b: str) -> str:
    return min(a, b, key=POLICIES.index)


def expand(store: Store, frontier: Frontier, scope: Scope, depth: int, run_id: int | None = None) -> Counter:
    if depth < 1:
        raise ValueError("expansion starts at depth 1")
    evo = scope.evolution
    kinds = {kind for kind in evo.follow if evo.depth_for(kind) >= depth}
    outcome = Counter()
    if not kinds:
        return outcome

    parents = [entry.item_id for entry in frontier.at_depth(depth - 1, "done", run_id)]
    with store.transaction():
        for hop in hops_from(store, parents, kinds, evo.direction):
            policy = evo.policy_for(hop.kind)
            if policy == "exclude":
                outcome["policy_exclude"] += 1
                continue

            listed = store.listing(hop.target_id)
            name = listed["name"] if listed is not None else hop.target_name
            if scope.names.excluded_token(name):
                outcome["excluded_name"] += 1
                continue

            entry_kind = policy
            if not evo.override_filters:
                if listed is None:
                    entry_kind = _weaker(policy, "reference")
                elif not decide(ListRow.from_db(listed), scope).keep:
                    outcome["filtered"] += 1
                    continue

            outcome[frontier.enqueue(Entry(
                hop.target_id, "evolution", entry_kind, f"evolution:{hop.kind}", depth, hop.parent_id), run_id)] += 1
    return outcome
