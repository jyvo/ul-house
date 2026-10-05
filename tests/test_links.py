import pytest

from fetch_fixtures import cached_path, missing
from ul_house.crawl.links import EvoLink, LinkMarkupError, read_links, record_links
from ul_house.seed.store import Store

# verified against the cached pages
EXPECTED = {
    "1015157": [("awakening", "after", "1015615"), ("awakening", "before", "1015065")],
    "1015655": [("reforge", "before", "1014667")],
    "1500502": [("reforge", "before", "1500501")],
    "1796604": [("reforge", "before", "1796603")],
    "1890424": [("reforge", "before", "1890423")],
    "4425111": [("awakening", "before", "4424110")],
    "4434015": [("reforge", "after", "4435013")],
    "4435013": [("enlightening", "after", "4435353"), ("reforge", "before", "4434015")],
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
    assert [(l.kind, l.side, l.target_id) for l in links] == EXPECTED[uid]
    assert all(l.source_id == uid and l.target_name for l in links)


def test_materials_are_not_links(pages):
    """1015157 links to seven equipment pages; two are lineage, the rest are materials"""
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

    def test_label_is_case_and_space_insensitive(self):
        (link,) = read_links(detail(slot("  BEFORE   Awakening ", "2")), "1")
        assert (link.kind, link.side, link.target_id) == ("awakening", "before", "2")

    @pytest.mark.parametrize("html, message", [
        (detail(slot("Before Rebirth", "2")), "unknown lineage label"),
        (detail(slot("Before Reforging", "1")), "itself"),
        (detail(slot("Before Reforging", "2"), slot("Before Reforging", "3")), "two"),
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
    assert EvoLink("1", "reforge", "before", "2", "x") == EvoLink("1", "reforge", "before", "2", "x")
