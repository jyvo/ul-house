import copy

import pytest

from ul_house.crawl.discover import ListRow
from ul_house.crawl.evolution import expand
from ul_house.crawl.frontier import Entry, Frontier
from ul_house.crawl.links import EvoLink
from ul_house.seed.store import Store
from ul_house.settings import Scope, load_scope

LIST = "/en/equip_list/1_5.html"


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "seed.sqlite") as s:
        s.record_response(LIST, "list", 200, b"<html/>")
        yield s


@pytest.fixture
def frontier(store):
    return Frontier(store, max_attempts=3)


def scope(**evolution):
    raw = copy.deepcopy(load_scope().to_mapping())
    raw["evolution"].update(evolution)
    if "per_chain" in evolution and evolution["per_chain"] is None:
        del raw["evolution"]["per_chain"]
    return Scope.from_mapping(raw)


def done(frontier, entry):
    frontier.enqueue(entry)
    claimed = frontier.claim_next()
    assert claimed.item_id == entry.item_id
    frontier.mark_done(entry.item_id)


def links(store, source, *specs):
    store.put_links(source, [EvoLink(source, kind, side, target, name) for kind, side, target, name in specs])


def kept(item_id):
    return Entry(item_id, "list", "catalog", "cost_band")


def listed(store, item_id, name="Item", cost=50, rarity="UR"):
    store.put_listing([ListRow(item_id, name, "weapon", rarity, "1", "2", cost, LIST)])


class TestTheWorkedChain:
    def seed(self, store, frontier):
        done(frontier, kept("1713802"))
        links(store, "1713802", ("awakening", "before", "1035607", "Middle"))
        links(store, "1035607", ("awakening", "before", "1035144", "First"))

    def test_depth_one_reaches_one_hop(self, store, frontier):
        self.seed(store, frontier)
        env = scope(per_chain=None, depth=1)
        assert expand(store, frontier, env, 1) == {"new": 1}
        assert frontier.get("1035607") == Entry("1035607", "evolution", "reference", "evolution:awakening", 1, "1713802")
        assert expand(store, frontier, env, 2) == {}
        assert frontier.get("1035144") is None

    def test_depth_two_reaches_the_root(self, store, frontier):
        self.seed(store, frontier)
        env = scope(per_chain=None, depth=2)
        expand(store, frontier, env, 1)
        assert frontier.claim_next().item_id == "1035607"
        frontier.mark_done("1035607")
        expand(store, frontier, env, 2)
        assert (frontier.get("1035144").depth, frontier.get("1035144").parent_id) == (2, "1035607")

    def test_unfetched_parents_do_not_expand(self, store, frontier):
        self.seed(store, frontier)
        env = scope(per_chain=None, depth=2)
        expand(store, frontier, env, 1)
        expand(store, frontier, env, 2)  # 1035607 is still pending
        assert frontier.get("1035144") is None


class TestPolicyAndFollow:
    def test_shipped_per_chain_policy(self, store, frontier):
        done(frontier, kept("A"))
        links(store, "A", ("reforge", "before", "R", "pre-reforge"), ("awakening", "before", "W", "pre-awake"))
        expand(store, frontier, load_scope(), 1)
        assert frontier.get("R").entry_kind == "catalog"      # reforge = { policy = "catalog" }
        assert frontier.get("W").entry_kind == "reference"

    def test_per_chain_depth(self, store, frontier):
        """enlightening = { depth = 2 }; the others stop at 1"""
        env = load_scope()
        done(frontier, kept("A"))
        links(store, "A", ("enlightening", "before", "E1", "e1"), ("reforge", "before", "R1", "r1"))
        expand(store, frontier, env, 1)
        for item_id in ("E1", "R1"):
            frontier.claim_next()
            frontier.mark_done(item_id)
        links(store, "E1", ("enlightening", "before", "E2", "e2"))
        links(store, "R1", ("reforge", "before", "R2", "r2"))
        expand(store, frontier, env, 2)
        assert frontier.get("E2") is not None
        assert frontier.get("R2") is None

    def test_exclude_policy(self, store, frontier):
        done(frontier, kept("A"))
        links(store, "A", ("awakening", "before", "W", "x"))
        assert expand(store, frontier, scope(per_chain=None, policy="exclude"), 1) == {"policy_exclude": 1}
        assert frontier.get("W") is None

    def test_unfollowed_kinds_ignored(self, store, frontier):
        done(frontier, kept("A"))
        links(store, "A", ("awakening", "before", "W", "x"))
        assert expand(store, frontier, scope(per_chain=None, follow=["reforge"]), 1) == {}

    def test_depth_zero_disables(self, store, frontier):
        done(frontier, kept("A"))
        links(store, "A", ("awakening", "before", "W", "x"))
        with pytest.warns(UserWarning):
            env = scope(per_chain=None, depth=0)
        assert expand(store, frontier, env, 1) == {}


class TestDirection:
    def seed(self, store, frontier):
        done(frontier, kept("B"))
        links(store, "B", ("reforge", "before", "A", "a"), ("reforge", "after", "C", "c"))
        links(store, "D", ("reforge", "before", "B", "b"))    # D names B as predecessor; B never names D

    def reached(self, store, frontier, direction):
        self.seed(store, frontier)
        expand(store, frontier, scope(direction=direction), 1)
        return {e for e in "ACD" if frontier.get(e) is not None}

    def test_predecessors(self, store, frontier):
        assert self.reached(store, frontier, "predecessors") == {"A"}

    def test_successors_include_inverted_before_edges(self, store, frontier):
        assert self.reached(store, frontier, "successors") == {"C", "D"}

    def test_both(self, store, frontier):
        assert self.reached(store, frontier, "both") == {"A", "C", "D"}


class TestFilters:
    def test_name_exclusion_wins_even_via_chain(self, store, frontier):
        done(frontier, kept("A"))
        links(store, "A", ("awakening", "before", "N", "[Awakening Ninoyu] Charm"))
        assert expand(store, frontier, load_scope(), 1) == {"excluded_name": 1}

    def test_listing_name_preferred(self, store, frontier):
        listed(store, "N", name="Awakening Ninoyu Charm")
        done(frontier, kept("A"))
        links(store, "A", ("awakening", "before", "N", "some other label"))
        assert expand(store, frontier, load_scope(), 1) == {"excluded_name": 1}

    def test_override_filters_true_ignores_cost(self, store, frontier):
        listed(store, "R", cost=10, rarity="SSR")
        done(frontier, kept("A"))
        links(store, "A", ("reforge", "before", "R", "r"))
        expand(store, frontier, load_scope(), 1)
        assert frontier.get("R").entry_kind == "catalog"

    def test_override_filters_false_applies_select(self, store, frontier):
        listed(store, "R", cost=10, rarity="SSR")
        done(frontier, kept("A"))
        links(store, "A", ("reforge", "before", "R", "r"))
        assert expand(store, frontier, scope(override_filters=False), 1) == {"filtered": 1}

    def test_override_filters_false_unlisted_reference_at_most(self, store, frontier):
        done(frontier, kept("A"))
        links(store, "A", ("reforge", "before", "U", "unlisted"))
        expand(store, frontier, scope(override_filters=False), 1)
        assert frontier.get("U").entry_kind == "reference"


class TestFrontierInteraction:
    def test_reaching_kept_item_never_downgrades_it(self, store, frontier):
        done(frontier, kept("A"))
        frontier.enqueue(kept("B"))
        links(store, "A", ("awakening", "before", "B", "b"))
        assert expand(store, frontier, load_scope(), 1) == {"kept": 1}
        assert frontier.get("B") == kept("B")

    def test_expansion_deterministic(self, store, frontier):
        done(frontier, kept("A"))
        done(frontier, kept("B"))
        links(store, "A", ("awakening", "before", "X", "x"))
        links(store, "B", ("awakening", "before", "X", "x"))
        expand(store, frontier, load_scope(), 1)
        assert frontier.get("X").parent_id == "A"

    def test_depth_must_be_positive(self, store, frontier):
        with pytest.raises(ValueError):
            expand(store, frontier, load_scope(), 0)


class TestRunAware:
    @pytest.fixture(autouse=True)
    def runs(self, store):
        for _ in range(2):
            store.start_run(trigger="manual", code_commit="c", catalog_version="v", catalog_commit="k",
                            shard_index=None)

    def test_expand_redecides_in_new_run(self, store, frontier):
        frontier.enqueue(kept("A"), run_id=2)
        frontier.claim_next(2)
        frontier.mark_done("A")
        frontier.enqueue(Entry("T", "list", "catalog", "cost_band"), run_id=1)
        links(store, "A", ("reforge", "before", "T", "Old"))
        out = expand(store, frontier, scope(), 1, run_id=2)
        assert out["redecided"] == 1
        assert frontier.decided_run("T") == 2
        target = frontier.get("T")
        assert (target.depth, target.parent_id, target.keep_reason) == (1, "A", "evolution:reforge")

    def test_expand_ignores_parents_of_other_runs(self, store, frontier):
        frontier.enqueue(kept("A"), run_id=1)
        frontier.claim_next(1)
        frontier.mark_done("A")
        links(store, "A", ("reforge", "before", "T", "Old"))
        assert expand(store, frontier, scope(), 1, run_id=2) == {}
        assert frontier.get("T") is None
        assert expand(store, frontier, scope(), 1, run_id=1)["new"] == 1

    def test_expand_without_run_is_unchanged(self, store, frontier):
        done(frontier, kept("A"))
        links(store, "A", ("reforge", "before", "T", "Old"))
        assert expand(store, frontier, scope(), 1)["new"] == 1
        assert frontier.decided_run("T") is None
