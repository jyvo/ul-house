import re

import pytest
from bs4 import BeautifulSoup

from fetch_fixtures import UIDS, cached_path, missing
from ul_house import config
from ul_house.crawl.discover import SOURCES, ListMarkupError, read_new_release, read_rows
from ul_house.crawl.links import read_links
from ul_house.http import robots as robots_mod
from ul_house.http.client import Client


WEAPON = "1015655"      # weapon ability, reforge lineage
AWAKENED = "1015157"    # special-evolution (awakening) material table
REFORGED = "4434015"    # reforge material table
MONSTER = "1796604"     # skills, potentials
LIVE_DETAIL = (WEAPON, AWAKENED, REFORGED, MONSTER)

EVERY_DETAIL_PAGE = {
    "HEADING_SELECTOR": config.HEADING_SELECTOR,
    "NAME_SELECTOR": config.NAME_SELECTOR,
    "BASIC_DATA_SELECTOR": config.BASIC_DATA_SELECTOR,
    "STATS_NAME_SELECTOR": config.STATS_NAME_SELECTOR,
    "STATS_SELECTOR": config.STATS_SELECTOR,
    "REFORGE_SELECTOR": config.REFORGE_SELECTOR,
    "EVO_LINK_LABEL_SELECTOR": config.EVO_LINK_LABEL_SELECTOR,
    "EVO_LINK_NAME_SELECTOR": config.EVO_LINK_NAME_SELECTOR,
    "SKILLS_SELECTOR[0]": config.SKILLS_SELECTOR[0],
    "SKILLS_SELECTOR[1]": config.SKILLS_SELECTOR[1],
}
ONLY_SOME_PAGES = {
    config.WEAPON_ABILITY_SELECTOR: WEAPON,
    config.SP_EVO_SELECTOR: AWAKENED,
    config.SP_MAT_TITLE_SELECTOR: AWAKENED,
    config.SP_MAT_CONTENT_SELECTOR: AWAKENED,
    config.REFORGE_MAT_SELECTOR: REFORGED,
}


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def check_detail_common(html: str, uid: str) -> None:
    soup = soup_of(html)
    for name, selector in EVERY_DETAIL_PAGE.items():
        assert soup.select(selector), f"{uid}: {name} {selector!r} matches nothing"
    assert len(soup.select(config.NAME_SELECTOR)) == 1, f"{uid}: {config.NAME_SELECTOR!r} must match exactly once"
    assert soup.select_one(config.NAME_SELECTOR).get_text(strip=True), f"{uid}: empty item name"


def check_detail_specific(html: str, uid: str) -> None:
    soup = soup_of(html)
    for selector, page in ONLY_SOME_PAGES.items():
        if page == uid:
            assert soup.select(selector), f"{uid}: {selector!r} matches nothing"


def check_lineage(html: str, uid: str) -> None:
    soup = soup_of(html)
    labels = [dt.get_text(" ", strip=True).casefold() for dt in soup.select(config.EVO_LINK_LABEL_SELECTOR)]
    assert labels, f"{uid}: no lineage labels"
    for label in labels:
        assert config.EVO_LINK_RE.match(label), f"{uid}: lineage label {label!r} no longer matches EVO_LINK_RE"
    read_links(html, uid)


def check_list_page(html: str, source) -> None:
    soup = soup_of(html)
    cells = soup.select(config.LIST_ROW_SELECTOR)
    assert cells, f"{source.path}: {config.LIST_ROW_SELECTOR!r} matches nothing"
    for cell in cells:
        assert cell.select_one(config.LIST_ROW_NAME_SELECTOR), f"{source.path}: row without {config.LIST_ROW_NAME_SELECTOR!r}"
        for field in ("cost", "attribute"):
            selector = config.LIST_ROW_INPUT.format(field=field)
            assert cell.select_one(selector), f"{source.path}: row without {selector!r}"
        anchor = cell.select_one("a[href]")
        assert anchor is not None and config.EQUIP_ID_RE.search(anchor["href"]), f"{source.path}: row without a detail link"
    rows = read_rows(html, source)               # the production reader agrees
    assert rows and all(row.cost > 0 and row.name for row in rows)


def check_new_release(html: str) -> None:
    assert read_new_release(html), "new-release page lists no equipment"


@pytest.fixture(scope="module")
def live():
    with Client(config.BASE_URL, interval=0.5, max_retries=2) as client:
        rules = robots_mod.load(client)
        assert rules.state != "unreachable", "robots.txt unreachable: the canary does not guess"
        robots_mod.apply(rules, client)
        yield client, rules


def fetch_live(live, path: str) -> str:
    client, rules = live
    assert rules.allowed(path), f"robots disallows {path}"
    reply = client.get(path)
    assert reply.status == 200, f"{path}: HTTP {reply.status}"
    assert reply.body, f"{path}: empty body"
    return reply.body.decode("utf-8", errors="replace")


@pytest.mark.live
class TestLiveSite:
    @pytest.mark.parametrize("uid", LIVE_DETAIL)
    def test_detail_selectors(self, live, uid):
        html = fetch_live(live, config.detail_path(uid))
        check_detail_common(html, uid)
        check_detail_specific(html, uid)

    @pytest.mark.parametrize("uid", LIVE_DETAIL)
    def test_lineage_labels_match_evo_link_re(self, live, uid):
        check_lineage(fetch_live(live, config.detail_path(uid)), uid)

    def test_icon_path_crawl_derives_still_serves_an_image(self, live):
        client, rules = live
        path = config.EQUIP_ICON_PATH.format(uid=WEAPON)
        assert rules.allowed(path)
        reply = client.get(path)
        assert reply.status == 200 and (reply.content_type or "").lower().startswith("image/"), (
            f"{path}: HTTP {reply.status} {reply.content_type!r}")
        assert reply.body and reply.body.startswith(b"\x89PNG"), f"{path}: not a PNG"

    @pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.path.rsplit("/", 1)[-1])
    def test_list_page_rows(self, live, source):
        check_list_page(fetch_live(live, source.path), source)

    def test_new_release_page(self, live):
        check_new_release(fetch_live(live, config.NEW_RELEASE_PATH))

    def test_list_row_links_to_detail_page_readable_by_crawl(self, live):
        source = SOURCES[0]
        rows = read_rows(fetch_live(live, source.path), source)
        uid = rows[0].item_id
        html = fetch_live(live, config.detail_path(uid))
        check_detail_common(html, uid)


@pytest.fixture(scope="module")
def cached():
    absent = missing()
    if absent:
        pytest.skip(f"fixture pages not cached: {', '.join(absent)}")
    return {uid: cached_path(uid).read_text() for uid in UIDS}


class TestCanaryChecksOnCachedPages:
    @pytest.mark.parametrize("uid", sorted(UIDS))
    def test_detail_checks_pass(self, cached, uid):
        check_detail_common(cached[uid], uid)
        check_detail_specific(cached[uid], uid)
        check_lineage(cached[uid], uid)

    def test_every_page_specific_selector_exercised(self, cached):
        for selector, uid in ONLY_SOME_PAGES.items():
            assert soup_of(cached[uid]).select(selector), (selector, uid)

    def test_checks_fail_on_drifted_markup(self, cached):
        html = cached[WEAPON]
        with pytest.raises(AssertionError, match="NAME_SELECTOR"):
            check_detail_common(html.replace("name__text", "title__text"), WEAPON)
        with pytest.raises(AssertionError, match="EVO_LINK_RE"):
            check_lineage(re.sub(r"Before Reforging", "Before Fusion", html, flags=re.I), WEAPON)

    def test_list_and_release_checks_on_synthetic_pages(self):
        from mini_seed import WikiItem, list_html, release_html

        row = WikiItem("1015655", "Test Blade", "weapon", "UR", 44)
        check_list_page(list_html([row]), SOURCES[0])
        check_new_release(release_html([("1015655", "Test Blade")]))
        with pytest.raises(AssertionError):
            check_list_page("<html><body>maintenance</body></html>", SOURCES[0])
        with pytest.raises(ListMarkupError):
            read_new_release("<html></html>")
