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
        assert frontier.enqueue_all([listed("1"), listed("2"), listed("1")]) == {"new": 2, "upgraded": 0, "kept": 1, "redecided": 0}


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


class TestMergeOnEachAxis:
    """kind and depth are strengthened independently"""

    def test_catalog_found_deeper(self, frontier):
        frontier.enqueue(reached("X", "A", depth=1, kind="reference"))
        assert frontier.enqueue(reached("X", "B", depth=2, kind="catalog")) == "upgraded"
        x = frontier.get("X")
        assert (x.entry_kind, x.depth, x.parent_id) == ("catalog", 1, "A")

    def test_shallower_reference_keeps_catalog(self, frontier):
        frontier.enqueue(reached("X", "B", depth=2, kind="catalog"))
        assert frontier.enqueue(reached("X", "A", depth=1, kind="reference")) == "upgraded"
        x = frontier.get("X")
        assert (x.entry_kind, x.depth, x.parent_id) == ("catalog", 1, "A")

    def test_equal_or_weaker_on_axes_is_kept(self, frontier):
        frontier.enqueue(reached("X", "A", depth=1, kind="catalog"))
        assert frontier.enqueue(reached("X", "B", depth=1, kind="catalog")) == "kept"
        assert frontier.enqueue(reached("X", "B", depth=2, kind="reference")) == "kept"
        assert frontier.get("X").parent_id == "A"

    def test_parent_always_shallower_than_child(self, frontier):
        frontier.enqueue_all([listed("A"), reached("X", "A", depth=1, kind="reference"),
                              reached("X", "B", depth=2, kind="catalog")])
        x = frontier.get("X")
        assert frontier.get(x.parent_id).depth < x.depth


class TestInterruptionIsNotAnAttempt:
    """interruption is not an attempt"""

    def test_interruptions_do_not_use_attempts(self, frontier):
        frontier.enqueue(listed("1"))
        for _ in range(5):
            frontier.claim_next()
            frontier.reset_in_flight()
        assert frontier.get("1").attempts == 0
        frontier.claim_next()
        assert frontier.mark_failed("1", "timeout") == "pending"

    def test_never_negative(self, frontier, store):
        frontier.enqueue(listed("1"))
        store.conn.execute("UPDATE frontier SET state = 'in_flight' WHERE item_id = '1'")
        frontier.reset_in_flight()
        assert frontier.get("1").attempts == 0


def start_runs(store, n):
    for _ in range(n):
        store.start_run(trigger="manual", code_commit="c", catalog_version="v", catalog_commit="k", shard_index=None)


@pytest.fixture
def runs(store):
    start_runs(store, 3)


class TestRunAware:
    def test_new_carries_run(self, frontier, runs):
        assert frontier.enqueue(listed("1"), run_id=1) == "new"
        assert frontier.decided_run("1") == 1
        assert frontier.decided_run("nope") is None

    def test_first_touch_in_new_replaces_decision(self, frontier, runs):
        frontier.enqueue(listed("1"), run_id=1)
        frontier.claim_next()
        frontier.mark_done("1")
        assert frontier.enqueue(reached("1", "9"), run_id=2) == "redecided"
        entry = frontier.get("1")
        assert (entry.entry_kind, entry.depth, entry.parent_id, entry.source) == ("reference", 1, "9", "evolution")
        assert (entry.state, entry.attempts) == ("done", 1)
        assert frontier.decided_run("1") == 2

    def test_same_run_merges(self, frontier, runs):
        assert frontier.enqueue(reached("2", "1"), run_id=2) == "new"
        assert frontier.enqueue(reached("2", "1", kind="catalog"), run_id=2) == "upgraded"
        assert frontier.enqueue(reached("2", "1"), run_id=2) == "kept"
        assert frontier.enqueue_all([reached("2", "1")], run_id=2)["kept"] == 1

    def test_run_none_is_legacy(self, frontier, runs):
        frontier.enqueue(listed("1"), run_id=1)
        assert frontier.enqueue(reached("1", "9")) == "kept"
        assert frontier.decided_run("1") == 1

    def test_claim_next_filters_by_run(self, frontier, runs):
        frontier.enqueue(listed("1"), run_id=1)
        frontier.enqueue(listed("2"), run_id=2)
        assert frontier.pending_count() == 2 and frontier.pending_count(2) == 1
        assert frontier.claim_next(2).item_id == "2"
        assert frontier.claim_next(2) is None
        assert frontier.pending_count(2) == 0 and frontier.pending_count(1) == 1

    def test_at_depth_filters_by_run(self, frontier, runs):
        frontier.enqueue_all([listed("1"), listed("2")], run_id=1)
        frontier.enqueue(listed("3"), run_id=2)
        for _ in range(3):
            frontier.mark_done(frontier.claim_next().item_id)
        assert [e.item_id for e in frontier.at_depth(0)] == ["1", "2", "3"]
        assert [e.item_id for e in frontier.at_depth(0, "done", 2)] == ["3"]

    def test_decided_lists_done_and_gone(self, frontier, runs):
        frontier.enqueue_all([listed("1"), listed("2"), listed("3"), listed("4")], run_id=1)
        frontier.enqueue(listed("5"), run_id=2)
        frontier.claim_next()
        frontier.mark_done("1")
        frontier.claim_next()
        frontier.mark_gone("2")
        frontier.claim_next()
        frontier.mark_failed("3", "x")           # back to pending
        assert [e.item_id for e in frontier.decided(1)] == ["1", "2"]
        assert [e.item_id for e in frontier.decided(1, ("gone",))] == ["2"]
        assert frontier.decided(2) == []

    def test_retry_failed(self, frontier):
        frontier.enqueue(listed("1"))
        for _ in range(3):
            frontier.claim_next()
            frontier.mark_failed("1", "timeout")
        assert frontier.get("1").state == "failed"
        assert frontier.retry_failed() == 1
        entry = frontier.get("1")
        assert (entry.state, entry.attempts, entry.last_error) == ("pending", 0, "timeout")
        assert frontier.retry_failed() == 0

    def test_mark_blocked_fails(self, frontier):
        frontier.enqueue(listed("1"))
        frontier.claim_next()
        frontier.mark_blocked("1", "contract: x")
        assert frontier.get("1").state == "failed" and frontier.get("1").last_error == "contract: x"
        with pytest.raises(ValueError, match="not in flight"):
            frontier.mark_blocked("1", "again")

    def test_revalidated_transitions(self, frontier):
        frontier.enqueue_all([listed("1"), listed("2")])
        with pytest.raises(ValueError):
            frontier.revalidated("1", "done")           #pending
        frontier.claim_next()
        with pytest.raises(ValueError):
            frontier.revalidated("1", "done")           #in flight
        frontier.mark_done("1")
        frontier.claim_next()
        frontier.mark_done("2")
        frontier.revalidated("1", "gone", "404")
        assert (frontier.get("1").state, frontier.get("1").last_error) == ("gone", "404")
        frontier.revalidated("1", "done")
        assert (frontier.get("1").state, frontier.get("1").last_error) == ("done", None)
        frontier.revalidated("2", "done", "http: 503")
        assert (frontier.get("2").state, frontier.get("2").last_error) == ("done", "http: 503")
        with pytest.raises(ValueError):
            frontier.revalidated("2", "failed")
