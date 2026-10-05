import pytest

from local_server import LocalServer, Reply
from ul_house.config import NEW_RELEASE_PATH
from ul_house.crawl.discover import (
    DiscoveryError,
    ListMarkupError,
    ListRow,
    ListSource,
    SOURCES,
    discover,
    read_new_release,
    read_rows,
)
from ul_house.http.client import Client
from ul_house.seed.store import Store

WEAPONS = ListSource("/en/equip_list/1_5.html", "weapon", "UR")
MONSTERS = ListSource("/en/equip_list/4_4.html", "monster", "SSR")


def cell(uid, name, cost, attribute="2", gear_type="1", **extra):
    inputs = []
    if gear_type is not None:
        inputs.append(f'<input name="unisonleague_type" type="hidden" value="{gear_type}"/>')
    inputs.append(f'<input name="unisonleague_attribute" type="hidden" value="{attribute}"/>')
    if cost is not None:
        inputs.append(f'<input name="unisonleague_cost" type="hidden" value="{cost}"/>')
    inputs.append('<input name="unisonleague_max_refining_count" type="hidden" value="5"/>')
    return (f'<td class="filter">{"".join(inputs)}<a href="/en/equip_detail/{uid}.html">'
            f'<div class="icons"><img class="icon lazyload" data-src="/images/equipicon/{uid}.png"/></div>'
            f'<p class="list_item_name">{name}</p></a></td>')


def page(*cells):
    return f'<html><body><table class="main"><tbody><tr>{"".join(cells)}</tr></tbody></table></body></html>'


def release_page(*pairs):
    tds = "".join(f'<td><a href="equip_detail/{uid}.html"><p class="list_item_name">{name}</p></a></td>'
                  for uid, name in pairs)
    return f'<html><body><table class="main"><tbody><tr>{tds}</tr></tbody></table></body></html>'


class TestReadRows:
    def test_weapon_row(self):
        (row,) = read_rows(page(cell("1015005", "Test Blade", 44, attribute="1", gear_type="1")), WEAPONS)
        assert row == ListRow("1015005", "Test Blade", "weapon", "UR", "1", "1", 44, WEAPONS.path)

    def test_group_and_rarity_come_from_the_source(self):
        (row,) = read_rows(page(cell("1", "x", 50)), MONSTERS)
        assert (row.grp, row.rarity, row.source_url) == ("monster", "SSR", MONSTERS.path)

    def test_monster_rows_have_no_type(self):
        (row,) = read_rows(page(cell("4000001", "Test Beast", 30, gear_type=None)), MONSTERS)
        assert row.gear_type is None

    def test_name_keeps_display_case(self):
        (row,) = read_rows(page(cell("1", "[Surging Sea] Sea Dragon's Sword", 44)), WEAPONS)
        assert row.name == "[Surging Sea] Sea Dragon's Sword"

    def test_order_and_duplicates(self):
        rows = read_rows(page(cell("2", "b", 50), cell("1", "a", 50), cell("2", "b again", 60)), WEAPONS)
        assert [(r.item_id, r.name) for r in rows] == [("2", "b"), ("1", "a")]

    @pytest.mark.parametrize("broken, message", [
        (cell("1", "x", None), "cost"),
        (cell("1", "x", "n/a"), "cost"),
        (cell("1", "", 50), "no name"),
        ('<td class="filter"><p class="list_item_name">x</p></td>', "no equip_detail link"),
    ])
    def test_broken_rows_are_contract_failures(self, broken, message):
        with pytest.raises(ListMarkupError, match=message):
            read_rows(page(cell("9", "fine", 50), broken), WEAPONS)

    def test_no_rows_at_all(self):
        with pytest.raises(ListMarkupError, match="no 'td.filter' rows"):
            read_rows("<html><body>maintenance</body></html>", WEAPONS)


class TestNewRelease:
    def test_relative_links(self):
        rows = read_new_release(release_page(("1399903", "A"), ("1399904", "B"), ("1399903", "A")))
        assert [(r.item_id, r.name) for r in rows] == [("1399903", "A"), ("1399904", "B")]

    def test_empty_is_a_contract_failure(self):
        with pytest.raises(ListMarkupError):
            read_new_release("<html></html>")


def test_sources_cover_all_group_and_rarity():
    assert {(s.grp, s.rarity) for s in SOURCES} == {
        (grp, rarity) for grp in ("weapon", "armor", "monster") for rarity in ("UR", "SSR")}


@pytest.fixture
def server():
    with LocalServer() as s:
        yield s


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "seed.sqlite") as s:
        yield s


def conditional(body: bytes, etag: str):
    def reply(request):
        if request.headers.get("If-None-Match") == etag:
            return Reply(304, headers={"ETag": etag})
        return Reply(200, body, {"ETag": etag, "Content-Type": "text/html; charset=utf-8"})
    return reply


def serve(server, weapons_html, releases=(("1", "a"),), etag='"w1"'):
    server.route(WEAPONS.path, conditional(weapons_html.encode(), etag))
    server.route(NEW_RELEASE_PATH, conditional(release_page(*releases).encode(), '"r1"'))


def client_for(server):
    return Client(server.base_url, interval=0.0, max_retries=0, timeout=(1, 1))


class TestDiscover:
    def test_first_run_stores_pages_and_listing(self, server, store):
        serve(server, page(cell("1", "a", 50), cell("2", "b", 60)))
        with client_for(server) as client:
            result = discover(client, store, (WEAPONS,))
        assert [r.item_id for r in result.rows] == ["1", "2"]
        assert result.changed == [WEAPONS.path, NEW_RELEASE_PATH]
        assert store.listing("2")["cost"] == 60
        assert store.page(WEAPONS.path)["etag"] == '"w1"'

    def test_second_run_conditional_and_replays_stored_html(self, server, store):
        serve(server, page(cell("1", "a", 50)))
        with client_for(server) as client:
            discover(client, store, (WEAPONS,))
            again = discover(client, store, (WEAPONS,))
        assert [r.item_id for r in again.rows] == ["1"]
        assert again.changed == []
        assert server.hits(WEAPONS.path)[1].headers["If-None-Match"] == '"w1"'
        assert store.page(WEAPONS.path)["status"] == 304

    def test_vanished_rows_reported_not_deleted(self, server, store):
        serve(server, page(cell("1", "a", 50), cell("2", "b", 60)))
        with client_for(server) as client:
            discover(client, store, (WEAPONS,))
            serve(server, page(cell("1", "a", 50)), etag='"w2"')
            result = discover(client, store, (WEAPONS,))
        assert result.vanished == ["2"]
        assert store.listing("2") is not None

    def test_unlisted_releases_reported(self, server, store):
        serve(server, page(cell("1", "a", 50)), releases=(("1", "a"), ("7", "brand new")))
        with client_for(server) as client:
            result = discover(client, store, (WEAPONS,))
        assert [(r.item_id, r.name) for r in result.unlisted_releases] == [("7", "brand new")]

    def test_http_failure_leaves_listing_untouched(self, server, store):
        serve(server, page(cell("1", "a", 50)))
        with client_for(server) as client:
            discover(client, store, (WEAPONS,))
            server.route(WEAPONS.path, Reply(503))
            with pytest.raises(DiscoveryError, match="HTTP 503"):
                discover(client, store, (WEAPONS,))
        assert store.listing("1") is not None
        assert store.page(WEAPONS.path)["etag"] == '"w1"'

    def test_markup_failure_does_not_advance_validators(self, server, store):
        serve(server, page(cell("1", "a", 50)))
        with client_for(server) as client:
            discover(client, store, (WEAPONS,))
            serve(server, "<html>maintenance</html>", etag='"w2"')
            with pytest.raises(ListMarkupError):
                discover(client, store, (WEAPONS,))
        assert store.page(WEAPONS.path)["etag"] == '"w1"'


class TestRunAware:
    def test_run_id_stamped_on_list_and_release_pages(self, server, store):
        serve(server, page(cell("1", "a", 50)))
        run = store.start_run(trigger="manual", code_commit="c", catalog_version="v", catalog_commit="k",
                              shard_index=None)
        with client_for(server) as client:
            discover(client, store, (WEAPONS,), run_id=run)
        assert store.page(WEAPONS.path)["run_id"] == run
        assert store.page(NEW_RELEASE_PATH)["run_id"] == run

    def test_no_run_id_is_legacy(self, server, store):
        serve(server, page(cell("1", "a", 50)))
        with client_for(server) as client:
            discover(client, store, (WEAPONS,))
        assert store.page(WEAPONS.path)["run_id"] is None

    def test_failed_new_release_read_skips_the_crosscheck_only(self, server, store):
        serve(server, page(cell("1", "a", 50)))
        server.route(NEW_RELEASE_PATH, Reply(503))
        with client_for(server) as client:
            result = discover(client, store, (WEAPONS,))
        assert [r.item_id for r in result.rows] == ["1"]
        assert result.unlisted_releases == [] and "HTTP 503" in result.new_release_error

    def test_malformed_new_release_page_skips_the_crosscheck_only(self, server, store):
        serve(server, page(cell("1", "a", 50)))
        server.route(NEW_RELEASE_PATH, conditional(b"<html>maintenance</html>", '"r2"'))
        with client_for(server) as client:
            result = discover(client, store, (WEAPONS,))
        assert result.new_release_error is not None
        assert store.page(NEW_RELEASE_PATH) is None
