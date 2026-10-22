import pytest

from fetch_fixtures import cached_path, missing
from ul_house.crawl.links import EvoLink, LinkMarkupError, read_links, record_links
from ul_house.seed.store import Store

# verified against the cached pages
EXPECTED = {
    "1015157": [("awakening", "1015065")],
    "1015655": [("reforge", "1014667")],
    "1500502": [("reforge", "1500501")],
    "1796604": [("reforge", "1796603")],
    "1890424": [("reforge", "1890423")],
    "4425111": [("awakening", "4424110")],
    "4434015": [],
    "4435013": [("reforge", "4434015")],
}


@pytest.fixture(scope="module")
def pages():
    absent = missing()
    if absent:
        pytest.skip(f"fixture pages not cached: {', '.join(absent)}")
    return {uid: cached_path(uid).read_text() for uid in EXPECTED}


@pytest.mark.parametrize("uid", sorted(EXPECTED))
def test_real_pages_yield_exactly_the_lineage(pages, uid):
    links = read_links(pages[uid], uid)
    assert [(l.kind, l.target_id) for l in links] == EXPECTED[uid]
    assert all(l.source_id == uid and l.target_name for l in links)


def test_materials_are_not_links(pages):
    """seven equipment pages, two lineage"""
    assert pages["1015157"].count("equip_detail/") > len(read_links(pages["1015157"], "1015157"))


def test_names_ride_along(pages):
    (link,) = read_links(pages["1015655"], "1015655")
    assert link.target_name == "Sea Dragon's Sword"


def slot(label, target=None, name="Target"):
    inner = (f'<a href="/en/equip_detail/{target}.html"><p class="evo_name">{name}</p></a>' if target
             else '<div class="detail__evo--none"><p class="detail__evo--block">-</p></div>')
    return (f'<dt class="detail__evo--last"><span>{label}</span></dt>'
            f'<dd class="detail__evo--last">{inner}</dd>')


def detail(*slots):
    return f'<html><body><dl class="detail__reincarnation">{"".join(slots)}</dl></body></html>'


class TestSynthetic:
    def test_placeholder_yields_nothing(self):
        assert read_links(detail(slot("Before Reforging"), slot("After Reforging")), "1") == []

    def test_two_after_slots_are_ignored(self):
        links = read_links(detail(slot("Before Reforging", "2"), slot("After Enlightening", "3"),
                                  slot("After Enlightening", "4")), "1")
        assert [(l.kind, l.target_id) for l in links] == [("reforge", "2")]

    @pytest.mark.parametrize("html", [
        detail(slot("After Reforging", "1")),                                     # links to itself
        '<dl><dt class="detail__evo--last"><span>After Reforging</span></dt></dl>',   # no dd
        detail(slot("After Reforging", "x").replace("/en/equip_detail/x.html", "/en/other/x.html")),
    ], ids=["self-link", "no-dd", "non-equipment-href"])
    def test_after_slot_contents_are_not_checked(self, html):
        assert read_links(html, "1") == []

    def test_label_is_case_and_space_insensitive(self):
        (link,) = read_links(detail(slot("  BEFORE   Awakening ", "2")), "1")
        assert (link.kind, link.target_id) == ("awakening", "2")

    @pytest.mark.parametrize("html, message", [
        (detail(slot("Before Rebirth", "2")), "unknown lineage label"),
        (detail(slot("Before Reforging", "1")), "itself"),
        (detail(slot("Before Reforging", "2"), slot("Before Reforging", "3")), "two"),
        (detail(slot("Before Enlightening", "2"), slot("Before Enlightening", "3")), "two 'before enlightening'"),
        ('<dl><dt class="detail__evo--last"><span>Before Reforging</span></dt></dl>', "no slot"),
    ])
    def test_contract_failures(self, html, message):
        with pytest.raises(LinkMarkupError, match=message):
            read_links(html, "1")

    def test_record_links_replaces(self, tmp_path):
        with Store.open(tmp_path / "s.sqlite") as store:
            record_links(store, "1", detail(slot("Before Reforging", "2")))
            record_links(store, "1", detail(slot("Before Awakening", "3")))
            assert [(r["kind"], r["target_id"]) for r in store.links_from(["1"])] == [("awakening", "3")]


def test_link_is_a_plain_record():
    assert EvoLink("1", "reforge", "2", "x") == EvoLink("1", "reforge", "2", "x")
