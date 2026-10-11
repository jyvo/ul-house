from __future__ import annotations

import posixpath
import re
from datetime import datetime, timedelta


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def check_current(current: dict) -> None:
    n = current["revision"]
    assert current["manifest"] == f"manifests/{n}.json", "INV-1"
    assert current["minimum_revision"] <= n, "INV-2"
    published, checked, expires = parse_ts(current["published_at"]), parse_ts(current["checked_at"]), parse_ts(current["expires_at"])
    assert published <= checked < expires, "INV-3 ordering"
    assert expires == checked + timedelta(days=14), "INV-3 expires_at = checked_at + 14 days"


def check_manifest(manifest: dict, current: dict) -> None:
    n = current["revision"]
    for field in ("revision", "minimum_revision", "dataset_schema_version", "api_schema_version"):
        assert manifest[field] == current[field], f"INV-2 {field}"
    art = manifest["artifacts"]
    assert art["snapshot"]["path"] == f"snapshots/unison-{n}.sqlite.gz", "INV-4 snapshot"
    assert art["lineage"]["path"] == f"lineage/{n}.json.gz", "INV-4 lineage"
    assert art["icon_packs"]["base"]["path"] == f"icons/packs/base-{n}.zip", "INV-4 base pack"
    froms = [u["from_revision"] for u in art["updates"]]
    for update in art["updates"]:
        assert update["path"] == f"updates/{update['from_revision']}-{n}.json.gz", "INV-4 update path"
    if manifest["snapshot_only"]:
        assert manifest["minimum_revision"] == n, "INV-4 snapshot_only => minimum_revision == N"
        assert froms == [] and art["icon_packs"]["deltas"] == []
    else:
        assert froms == list(range(manifest["minimum_revision"], n)), "INV-4 updates cover [minimum_revision, N)"
    assert froms == sorted(froms)
    delta_froms = [d["from_revision"] for d in art["icon_packs"]["deltas"]]
    for delta in art["icon_packs"]["deltas"]:
        assert delta["from_revision"] in froms, "INV-5 delta from is an update from"
        assert delta["path"] == f"icons/packs/delta-{delta['from_revision']}-{n}.zip", "INV-5 delta path"
    assert delta_froms == sorted(delta_froms)
    assert manifest["entity_counts"]["icon"] == manifest["icons"]["count"], "INV-9"


def check_package(package: dict, manifest: dict, pack_index: dict | None = None) -> None:
    n = manifest["revision"]
    lo, hi = package["from_revision"], package["to_revision"]
    assert manifest["minimum_revision"] <= lo < hi == n, "INV-6 revision window"
    seen: set[tuple[str, str, str]] = set()
    retired_at: dict[str, int] = {}
    for position, op in enumerate(package["ops"]):
        assert lo < op["rev"] <= hi, f"INV-6 op.rev outside ({lo}, {hi}]"
        identity = (op["op"], op["entity"], op["key"])
        assert identity not in seen, f"INV-6 duplicate op {identity}"
        seen.add(identity)
        key = op["key"]
        if op["op"] == "RETIRE":
            retired_at[key] = position
        if op["op"] == "ALIAS":
            assert op["to"] != key, "INV-6 ALIAS to itself"
            if key in retired_at:
                assert retired_at[key] < position, "INV-6 RETIRE precedes ALIAS"
        if op["op"] != "UPSERT":
            continue
        data = op["data"]
        if op["entity"] == "equipment":
            assert data["equipment"]["uid"] == key, "INV-6 equipment key"
            for table in ("evolution_edge", "evolution_material"):
                for row in data[table]:
                    assert key in (row["from_uid"], row["to_uid"]), f"INV-6 {table} row does not touch {key}"
        if op["entity"] == "uid_retired":
            assert data["uid_retired"]["uid"] == key, "INV-6 uid_retired key"
            assert lo < data["uid_retired"]["since_revision"] <= hi
        if op["entity"] == "uid_alias":
            assert data["uid_alias"]["old_uid"] == key, "INV-6 uid_alias key"
            assert lo < data["uid_alias"]["since_revision"] <= hi
    for position, op in enumerate(package["ops"]):
        if op["op"] == "ALIAS" and op["key"] in retired_at:
            assert retired_at[op["key"]] < position
    icons = package["icons"]
    deltas = {d["from_revision"]: d for d in manifest["artifacts"]["icon_packs"]["deltas"]}
    if icons["added"]:
        delta = deltas[lo]
        assert icons["pack"] == delta["path"] and icons["pack_sha256"] == delta["sha256"], "INV-7 pack matches manifest"
        if pack_index is not None:
            assert set(icons["added"]) == {i["sha256"] for i in pack_index["icons"]}, "INV-7 added == index.json shas"
    else:
        assert icons["pack"] is None and icons["pack_sha256"] is None
        assert lo not in deltas, "INV-5 no delta pack when nothing was added"


def check_pack(index: dict, members: dict[str, bytes], names_in_order: list[str], manifest: dict, pack_path: str) -> None:
    """invariant 8, index.json first, one member per icons[] at {kind}/{sha256}.png, sha and size match"""
    import hashlib

    assert names_in_order[0] == "index.json", "INV-8 index.json is the first member"
    assert names_in_order[1:] == [i["path"] for i in index["icons"]], "INV-8 exactly one member per icons[] entry"
    assert index["name"] == posixpath.basename(pack_path), "INV-8 name is the basename"
    assert index["to_revision"] == manifest["revision"], "INV-8 to_revision"
    for icon in index["icons"]:
        assert icon["path"] == f"{icon['kind']}/{icon['sha256']}.png"
        data = members[icon["path"]]
        assert hashlib.sha256(data).hexdigest() == icon["sha256"], "INV-8 member sha256"
        assert len(data) == icon["bytes"], "INV-8 member size"
    packs = manifest["artifacts"]["icon_packs"]
    if index["pack_type"] == "base":
        assert packs["base"]["icon_count"] == len(index["icons"]), "INV-8 base icon_count"
    else:
        delta = next(d for d in packs["deltas"] if d["path"] == pack_path)
        assert delta["from_revision"] == index["from_revision"]
        assert delta["icon_count"] == len(index["icons"])


def api_path(relative: str) -> str:
    kinds = [
        (r"current\.json", lambda m: "/v1/version"),
        (r"manifests/([1-9][0-9]*)\.json", lambda m: f"/v1/manifests/{m[1]}"),
        (r"snapshots/unison-([1-9][0-9]*)\.sqlite\.gz", lambda m: f"/v1/snapshots/{m[1]}"),
        (r"lineage/([1-9][0-9]*)\.json\.gz", lambda m: f"/v1/lineage/{m[1]}"),
        (r"icons/packs/(.+\.zip)", lambda m: f"/v1/icons/packs/{m[1]}"),
        (r"icons/(equipment|item|ability)/([0-9a-f]{64})\.png", lambda m: f"/v1/icons/{m[1]}/{m[2]}"),
        (r"updates/([0-9]+)-[1-9][0-9]*\.json\.gz", lambda m: f"/v1/updates/{m[1]}"),
    ]
    for pattern, build in kinds:
        m = re.fullmatch(pattern, relative)
        if m:
            return build(m)
    raise AssertionError(relative)
