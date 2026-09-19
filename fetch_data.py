import requests
from bs4 import BeautifulSoup, Tag

from config import SP_EVO_SELECTOR
from parser import detail_url, _parse_name, _parse_data, _parse_stats, _parse_skills, _parse_ability, _parse_reforge, _parse_awakening


def fetch_item(item_id: str):
    response = requests.get(detail_url(item_id), headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")

    # print(item_id)
    # print(soup.select_one(SP_EVO_SELECTOR))

    return {
        "id": item_id,
        "name": _parse_name(soup),
        "url": detail_url(item_id),
        "data": _parse_data(soup),
        "stats": _parse_stats(soup),
        "skills": _parse_skills(soup),
        "ability": _parse_ability(soup),
        "reforge": _parse_reforge(soup),
        "awakening": _parse_awakening(soup),
    }


def main():
    item_ids = [
        "1015655", #ur (weapon skill)
        "1014667", #ssr pre reforge of above
        "1015157", # xeno
        "1796604", #mon (passive)
        "1500502", #fatewoven
        "4425111", #mon (awakening)
        "4435013", #mon (enlightening)
    ]

    items = []
    for item_id in item_ids:
        item = fetch_item(item_id)
        print(f"{item}\n")
        items.append(item)


if __name__ == "__main__":
    main()
