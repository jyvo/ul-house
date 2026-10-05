"""icon assets"""
from __future__ import annotations

from dataclasses import dataclass

from ul_house.config import EQUIP_ICON_PATH
from ul_house.http.client import Client, FetchError
from ul_house.http.robots import Robots
from ul_house.seed.store import Store

IMAGE_TYPE_PREFIX = "image/"
EQUIPMENT = "equipment"


@dataclass(frozen=True)
class IconTarget:
    url: str
    item_id: str
    kind: str


@dataclass(frozen=True)
class IconFetch:
    url: str
    status: int | None
    changed: bool
    outcome: str            # changed | unchanged | not_modified | missing | failed


def icon_targets(uid: str, html: str = "") -> list[tuple[str, str]]:
    """[(asset kind, url)], icon derived from uid"""
    return [(t.kind, t.url) for t in _targets(uid)]


def _targets(item_id: str) -> list[IconTarget]:
    return [IconTarget(EQUIP_ICON_PATH.format(uid=item_id), item_id, EQUIPMENT)]


def record_targets(store: Store, item_id: str, html: str) -> list[IconTarget]:
    """derive and store"""
    targets = _targets(item_id)
    store.put_asset_targets(item_id, targets)
    return targets


def _is_image(content_type: str | None) -> bool:
    return (content_type or "").strip().lower().startswith(IMAGE_TYPE_PREFIX)


def fetch_icon(client: Client, store: Store, url: str, *, run_id: int | None = None, robots: Robots | None = None) -> IconFetch:
    if robots is not None and not robots.allowed(url):
        return _finish(store, run_id, IconFetch(url, None, False, "failed"), lambda: None)

    validators = store.asset_validators(url)
    try:
        reply = client.get(url, validators.headers() if validators else None)
    except FetchError:
        return _finish(store, run_id, IconFetch(url, None, False, "failed"), lambda: None)

    status = reply.status
    if status == 200 and _is_image(reply.content_type) and reply.body:
        with store.transaction():
            changed = store.record_asset(url, 200, reply.body, reply.etag, reply.last_modified, reply.content_type, run_id=run_id)
            result = IconFetch(url, 200, changed, "changed" if changed else "unchanged")
            _bump(store, run_id, result)
        return result

    if status == 304 and validators is not None:
        with store.transaction():
            store.record_asset(url, 304, etag=reply.etag, last_modified=reply.last_modified, run_id=run_id)
            result = IconFetch(url, 304, False, "not_modified")
            _bump(store, run_id, result)
        return result

    outcome = "missing" if status == 404 else "failed"
    result = IconFetch(url, status, False, outcome)
    return _finish(store, run_id, result, lambda: store.record_asset_status(url, status, run_id=run_id))


def _bump(store: Store, run_id: int | None, result: IconFetch) -> None:
    if run_id is None:
        return
    store.bump_run(
        run_id,
        icons_fetched=int(result.status is not None),
        icons_changed=int(result.changed),
        icons_failed=int(result.outcome in ("missing", "failed")),
    )


def _finish(store: Store, run_id: int | None, result: IconFetch, write) -> IconFetch:
    with store.transaction():
        write()
        _bump(store, run_id, result)
    return result


def fetch_icons(client: Client, store: Store, item_id: str, *, run_id: int | None = None,
                robots: Robots | None = None, only_unfetched: bool = False) -> list[IconFetch]:
    """every asset of item_id | only those never requested"""
    rows = store.assets_for(item_id)
    if only_unfetched:
        rows = [row for row in rows if row["fetched_at"] is None]
    return [fetch_icon(client, store, row["url"], run_id=run_id, robots=robots) for row in rows]
