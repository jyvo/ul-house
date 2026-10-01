import pytest

from ul_house.crawl.frontier import Entry, Frontier
from ul_house.seed.store import Store


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "seed.sqlite") as s:
        yield s


@pytest.fixture
def frontier(store):
    return Frontier(store, max_attempts=3)


def listed(item_id, **kw):
    return Entry(item_id, "list", "catalog", "cost_band", **kw)


def reached(item_id, parent, depth=1, kind="reference"):
    return Entry(item_id, "evolution", kind, "evolution:reforge", depth, parent)


class TestEnqueue:
    def test_new(self, frontier):
        assert frontier.enqueue(listed("1")) == "new"
        assert frontier.get("1").state == "pending"

    def test_catalog_over_ref(self, frontier):
        frontier.enqueue(reached("2", "1"))
        assert frontier.enqueue(reached("2", "1", kind="catalog")) == "upgraded"
        assert frontier.get("2").entry_kind == "catalog"

    def test_shallower_over_deeper(self, frontier):
        frontier.enqueue(reached("3", "2", depth=2))
        assert frontier.enqueue(reached("3", "1", depth=1)) == "upgraded"
        assert (frontier.get("3").depth, frontier.get("3").parent_id) == (1, "1")

    def test_never_downgraded(self, frontier):
        frontier.enqueue(listed("1"))
        assert frontier.enqueue(reached("1", "9")) == "kept"
        assert frontier.get("1") == listed("1")

    def test_upgrade_leaves_page_done(self, frontier):
        frontier.enqueue(reached("2", "1"))
        frontier.claim_next()
        frontier.mark_done("2")
        frontier.enqueue(listed("2"))
        assert frontier.get("2").state == "done"
        assert frontier.get("2").entry_kind == "catalog"

    def test_depth_and_parent_agree(self, frontier, store):
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            frontier.enqueue(Entry("1", "evolution", "reference", "x", depth=1, parent_id=None))

    def test_rejects_unknown_vocab(self, frontier):
        with pytest.raises(ValueError):
            frontier.enqueue(Entry("1", "rumour", "catalog", "x"))
        with pytest.raises(ValueError):
            frontier.enqueue(Entry("1", "list", "stub", "x"))

    def test_enqueue_all_counts(self, frontier):
        assert frontier.enqueue_all([listed("1"), listed("2"), listed("1")]) == {"new": 2, "upgraded": 0, "kept": 1}


class TestClaim:
    def test_order_depth_then_id(self, frontier):
        frontier.enqueue_all([reached("5", "1"), listed("9"), listed("1")])
        order = []
        while (entry := frontier.claim_next()) is not None:
            order.append(entry.item_id)
            frontier.mark_done(entry.item_id)
        assert order == ["1", "9", "5"]

    def test_claim_counts_an_attempt(self, frontier):
        frontier.enqueue(listed("1"))
        entry = frontier.claim_next()
        assert (entry.state, entry.attempts) == ("in_flight", 1)

    def test_empty(self, frontier):
        assert frontier.claim_next() is None

    def test_transitions_needs_claim(self, frontier):
        frontier.enqueue(listed("1"))
        with pytest.raises(ValueError, match="not in flight"):
            frontier.mark_done("1")


class TestResume:
    def test_interrupted_claim_retried(self, tmp_path):
        path = tmp_path / "seed.sqlite"
        with Store.open(path) as store:
            frontier = Frontier(store, 3)
            frontier.enqueue_all([listed("1"), listed("2")])
            frontier.claim_next()  # the process dies here, mid-request

        with Store.open(path) as store:
            frontier = Frontier(store, 3)
            assert frontier.reset_in_flight() == 1
            assert frontier.claim_next().item_id == "1"


class TestFailure:
    def test_retried_until_attempts_out(self, frontier):
        frontier.enqueue(listed("1"))
        states = []
        for _ in range(3):
            frontier.claim_next()
            states.append(frontier.mark_failed("1", "timeout"))
        assert states == ["pending", "pending", "failed"]
        assert frontier.get("1").last_error == "timeout"
        assert frontier.claim_next() is None

    def test_gone(self, frontier):
        frontier.enqueue(listed("1"))
        frontier.claim_next()
        frontier.mark_gone("1")
        assert frontier.counts() == {"gone": 1}

    def test_max_attempts_positive(self, store):
        with pytest.raises(ValueError):
            Frontier(store, 0)

    def test_at_depth(self, frontier):
        frontier.enqueue_all([listed("1"), reached("2", "1")])
        frontier.claim_next()
        frontier.mark_done("1")
        assert [e.item_id for e in frontier.at_depth(0)] == ["1"]
        assert frontier.at_depth(1) == []
