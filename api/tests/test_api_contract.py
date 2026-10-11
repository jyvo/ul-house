from __future__ import annotations

import copy
import gzip
import json
import re
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from api_common import SCHEMA_NAMES, SCHEMAS

SHARED_DEFS = ("revision", "schema_version", "sha256", "timestamp", "commit")
ID_BASE = "https://schemas.ul-house.invalid/api/v1/"
IMMUTABLE_OPS = {"getManifest", "getSnapshot", "getIconPack", "getIcon", "getLineage"}
POINTER_OPS = {"getVersion", "getUpdate"}
EXPECTED_PATHS = [
    "/v1/version",
    "/v1/manifests/{revision}",
    "/v1/updates/{from_revision}",
    "/v1/snapshots/{revision}",
    "/v1/icons/packs/{name}",
    "/v1/icons/{kind}/{sha256}",
    "/v1/lineage/{revision}",
]


def schema_file(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def walk_refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from walk_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_refs(item)


def pointer(document: dict, ref: str):
    node = document
    for part in ref.removeprefix("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def deref(openapi: dict, node):
    while isinstance(node, dict) and "$ref" in node and node["$ref"].startswith("#/"):
        node = pointer(openapi, node["$ref"])
    return node


def operations(openapi: dict):
    for path, item in openapi["paths"].items():
        yield path, item["get"]


# schema files
@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_a_valid_2020_12_schema(name):
    Draft202012Validator.check_schema(schema_file(name))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_id_dialect_and_title(name):
    schema = schema_file(name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"{ID_BASE}{name}.schema.json"
    assert schema["title"]


def test_schema_ids_are_unique(contract):
    ids = [schema["$id"] for schema in contract.schemas.values()]
    assert len(ids) == len(set(ids)) == 5


def test_schema_files_are_exactly_the_five_of_p8():
    assert sorted(path.name for path in SCHEMAS.iterdir()) == sorted(f"{name}.schema.json" for name in SCHEMA_NAMES)


@pytest.mark.parametrize("definition", SHARED_DEFS)
def test_shared_defs_are_identical_wherever_present(contract, definition):
    copies = {name: schema["$defs"][definition] for name, schema in contract.schemas.items() if definition in schema.get("$defs", {})}
    assert len(copies) >= 2, f"{definition} should be shared by at least two schemas, found {sorted(copies)}"
    first_name, first = next(iter(copies.items()))
    for name, copy_ in copies.items():
        assert copy_ == first, f"$defs.{definition} differs between {first_name} and {name}"


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schemas_are_self_contained(contract, name):
    schema = contract.schemas[name]
    for ref in walk_refs(schema):
        assert ref.startswith("#/"), f"{name}: cross-file $ref {ref}"
        pointer(schema, ref)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_objects_are_strict(contract, name):
    schema = contract.schemas[name]
    assert schema.get("additionalProperties") is False, f"{name}: top level must be additionalProperties:false"


# openapi yaml
def test_openapi_version_and_info(contract):
    assert contract.openapi["openapi"] == "3.1.0"
    assert contract.openapi["info"]["version"] == "1"
    assert contract.openapi["jsonSchemaDialect"] == "https://json-schema.org/draft/2020-12/schema"


def test_openapi_paths_are_exactly_the_seven_get_routes(contract):
    paths = list(contract.openapi["paths"])
    assert sorted(paths) == sorted(EXPECTED_PATHS)
    for path, item in contract.openapi["paths"].items():
        assert set(item) - {"parameters", "summary", "description"} == {"get"}, path


def test_icon_packs_are_declared_before_the_kind_route(contract):
    paths = list(contract.openapi["paths"])
    assert paths.index("/v1/icons/packs/{name}") < paths.index("/v1/icons/{kind}/{sha256}")


def test_openapi_refs_resolve(contract):
    for ref in walk_refs(contract.openapi):
        if ref.startswith("#/"):
            pointer(contract.openapi, ref)
        else:
            assert ref.startswith("schemas/") and (SCHEMAS.parent / ref).is_file(), ref


def test_operation_ids_are_unique(contract):
    ids = [operation["operationId"] for _, operation in operations(contract.openapi)]
    assert len(ids) == len(set(ids)) == 7


def test_every_operation_has_a_pub_key(contract):
    for path, operation in operations(contract.openapi):
        assert operation.get("x-pub-key"), path
    keys = {path: op["x-pub-key"] for path, op in operations(contract.openapi)}
    assert keys["/v1/version"] == "current.json"
    assert keys["/v1/snapshots/{revision}"] == "snapshots/unison-{revision}.sqlite.gz"


def test_every_response_declares_x_api_commit(contract):
    openapi = contract.openapi
    for path, operation in operations(openapi):
        for status, response in operation["responses"].items():
            resolved = deref(openapi, response)
            assert "X-API-Commit" in resolved.get("headers", {}), f"{path} {status}"
    for name in ("NotModifiedPointer", "NotModifiedUpdates", "NotModifiedImmutable", "NotFound", "NotFoundOrNoUpdate", "MethodNotAllowed", "Unavailable", "Internal"):
        assert "X-API-Commit" in openapi["components"]["responses"][name]["headers"], name


def header_const(openapi: dict, response: dict, header: str):
    resolved = deref(openapi, response)
    return deref(openapi, resolved["headers"][header])["schema"]["const"]


def test_cache_headers_per_route(contract):
    openapi = contract.openapi
    for path, operation in operations(openapi):
        op_id = operation["operationId"]
        ok = operation["responses"]["200"]
        not_modified = operation["responses"]["304"]
        if op_id in IMMUTABLE_OPS:
            expected = "public, max-age=31536000, immutable"
        else:
            assert op_id in POINTER_OPS
            expected = "public, max-age=300"
        assert header_const(openapi, ok, "Cache-Control") == expected, f"{path} 200"
        assert header_const(openapi, not_modified, "Cache-Control") == expected, f"{path} 304"


def test_error_responses_are_no_store(contract):
    openapi = contract.openapi
    for name in ("NotFound", "NotFoundOrNoUpdate", "MethodNotAllowed", "Unavailable", "Internal"):
        response = openapi["components"]["responses"][name]
        assert header_const(openapi, response, "Cache-Control") == "no-store", name
    assert header_const(openapi, openapi["components"]["responses"]["MethodNotAllowed"], "Allow") == "GET"


def test_nosniff_is_declared_on_every_304_and_error_response(contract):
    openapi = contract.openapi
    for name in ("NotModifiedPointer", "NotModifiedUpdates", "NotModifiedImmutable", "NotFound", "NotFoundOrNoUpdate", "MethodNotAllowed", "Unavailable", "Internal"):
        assert "X-Content-Type-Options" in openapi["components"]["responses"][name]["headers"], name
    updates = openapi["paths"]["/v1/updates/{from_revision}"]["get"]["responses"]["304"]
    assert "X-UL-To-Revision" in deref(openapi, updates)["headers"]


def test_updates_documents_the_to_revision_header_and_both_content_types(contract):
    ok = contract.openapi["paths"]["/v1/updates/{from_revision}"]["get"]["responses"]["200"]
    assert "X-UL-To-Revision" in ok["headers"]
    assert set(ok["content"]) == {"application/gzip", "application/json"}
    assert contract.decoded_schema("/v1/updates/{from_revision}", "application/gzip") is contract.schemas["update_package"]
    assert contract.decoded_schema("/v1/updates/{from_revision}", "application/json") is contract.schemas["requires_snapshot"]
    assert contract.decoded_schema("/v1/version", "application/json") is contract.schemas["current"]


def test_error_codes_enum(contract):
    assert set(contract.error_schema["properties"]["error"]["enum"]) == {"not_found", "no_update", "method_not_allowed", "unavailable", "internal"}


# openapi patterns == routing.py
def norm(pattern: str) -> str:
    return pattern.removeprefix("^").removesuffix("$").replace("(?:", "(")


def test_path_parameter_patterns_equal_routing(contract):
    from syncapi import routing

    params = contract.openapi["components"]["parameters"]
    assert norm(params["Revision"]["schema"]["pattern"]) == norm(routing.REV)
    assert norm(params["FromRevision"]["schema"]["pattern"]) == norm(routing.FROM_REV)
    assert norm(params["Sha256"]["schema"]["pattern"]) == norm(routing.SHA)
    assert norm(params["PackName"]["schema"]["pattern"]) == norm(routing.PACK)
    assert set(params["IconKind"]["schema"]["enum"]) == set(norm(routing.KIND).strip("()").split("|")) == {"equipment", "item", "ability"}


# examples
@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_examples_validate(contract, name):
    for example in contract.schemas[name].get("examples", []):
        contract.validate(name, example)


def test_every_schema_documents_an_example(contract):
    assert set(SCHEMA_NAMES) == {"current", "manifest", "requires_snapshot", "update_package", "icon_pack"}
    for name in SCHEMA_NAMES:
        assert contract.schemas[name].get("examples"), name


@pytest.fixture(scope="module")
def docs(store):
    pub = store.root / "pub"
    package = json.loads(gzip.decompress((pub / "updates" / "1-2.json.gz").read_bytes()))
    return {
        "current": json.loads((pub / "current.json").read_bytes()),
        "manifest": json.loads((pub / "manifests" / "2.json").read_bytes()),
        "update_package": package,
        "base": json.loads(zipfile.ZipFile(pub / "icons" / "packs" / "base-2.zip").read("index.json")),
        "delta": json.loads(zipfile.ZipFile(pub / "icons" / "packs" / "delta-1-2.zip").read("index.json")),
        "requires_snapshot": {"requires_snapshot": True, "snapshot_revision": 2, "from_revision": 0, "reason": "below_minimum"},
        "error": {"error": "not_found", "message": "not found"},
    }


def test_baseline_instances_are_valid(contract, docs):
    contract.validate("current", docs["current"])
    contract.validate("manifest", docs["manifest"])
    contract.validate("update_package", docs["update_package"])
    contract.validate("icon_pack", docs["base"])
    contract.validate("icon_pack", docs["delta"])
    contract.validate("requires_snapshot", docs["requires_snapshot"])
    contract.validate_error(docs["error"])


@pytest.mark.parametrize("reason", ["package_size", "dataset_schema_version", "catalog_version", "forced", "first_release", "no_base"])
def test_snapshot_only_reasons_accepted(contract, docs, reason):
    manifest = copy.deepcopy(docs["manifest"])
    manifest["snapshot_only"] = True
    manifest["snapshot_only_reasons"] = [reason]
    manifest["minimum_revision"] = manifest["revision"]
    manifest["artifacts"]["updates"] = []
    manifest["artifacts"]["icon_packs"]["deltas"] = []
    contract.validate("manifest", manifest)


@pytest.mark.parametrize("reason", ["below_minimum", "package_missing"])
def test_requires_snapshot_reasons_accepted(contract, docs, reason):
    contract.validate("requires_snapshot", {**docs["requires_snapshot"], "reason": reason})


def test_package_with_every_op_kind_is_valid(contract, docs):
    package = copy.deepcopy(docs["update_package"])
    package["ops"] += [
        {"op": "ALIAS", "entity": "equipment", "key": "1000004", "to": "1000005", "rev": 2, "confirmed_commit": "a" * 40},
        {"op": "DELETE", "entity": "proc", "key": "123456789", "rev": 2},
        {"op": "DELETE", "entity": "icon", "key": "b" * 64, "rev": 2},
        {"op": "DELETE", "entity": "element", "key": "fire", "rev": 2},
        {"op": "UPSERT", "entity": "uid_alias", "key": "1000004", "rev": 2,
         "data": {"uid_alias": {"old_uid": "1000004", "new_uid": "1000005", "since_revision": 2, "confirmed_commit": "a" * 40}}},
        {"op": "UPSERT", "entity": "skill_effect", "key": "0", "rev": 2, "data": {"skill_effect": {"effect_id": "0", "text": "x"}}},
        {"op": "UPSERT", "entity": "proc", "key": "9223372036854775807", "rev": 2,
         "data": {"proc": {"proc_id": "9223372036854775807"}, "proc_condition": [], "proc_scaling": [], "effect_link": []}},
    ]
    contract.validate("update_package", package)


def test_package_with_no_icons_added_uses_the_null_form(contract, docs):
    package = copy.deepcopy(docs["update_package"])
    package["icons"] = {"pack": None, "pack_sha256": None, "added": []}
    contract.validate("update_package", package)


def test_equipment_aggregate_without_subtype_is_valid(contract, docs):
    package = copy.deepcopy(docs["update_package"])
    equipment = next(op for op in package["ops"] if op["entity"] == "equipment" and op["op"] == "UPSERT")
    equipment["data"].pop("weapon")
    contract.validate("update_package", package)


def test_package_entity_enum_is_the_eleven_types_of_p21(contract):
    entity = contract.schemas["update_package"]["$defs"]["entity"]["enum"]
    assert set(entity) == {"equipment", "item", "weapon_ability", "proc_family", "proc", "skill_effect", "passive_skill", "icon", "element", "uid_retired", "uid_alias"}
    deletable = contract.schemas["update_package"]["$defs"]["deletable_entity"]["enum"]
    assert not {"equipment", "uid_retired", "uid_alias"} & set(deletable)


def test_manifest_requires_all_24_tables(contract):
    counts = contract.schemas["manifest"]["properties"]["entity_counts"]
    assert len(counts["required"]) == 24 and set(counts["required"]) == set(counts["properties"])


# mutations
def _set(path, value):
    def mutate(doc):
        node = doc
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
    return mutate


def _del(path):
    def mutate(doc):
        node = doc
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]
    return mutate


def _op(entity, op="UPSERT"):
    def find(doc):
        return next(o for o in doc["ops"] if o["entity"] == entity and o["op"] == op)
    return find


def _equipment_data(doc):
    return _op("equipment")(doc)["data"]


def _retire_item(doc):
    _op("equipment", "RETIRE")(doc)["entity"] = "item"


def _alias_missing(field):
    def mutate(doc):
        doc["ops"].append({"op": "ALIAS", "entity": "equipment", "key": "1000004", "to": "1000005", "rev": 2, "confirmed_commit": "a" * 40})
        del doc["ops"][-1][field]
    return mutate


def _append(op):
    def mutate(doc):
        doc["ops"].append(op)
    return mutate


def _nested_row_value(doc):
    _equipment_data(doc)["stat"][0]["value"] = {"nested": 1}


def _weapon_and_monster(doc):
    _equipment_data(doc)["monster"] = {"uid": "1000001"}


def _drop_stat(doc):
    del _equipment_data(doc)["stat"]


def _retire_without_reason(doc):
    del _op("equipment", "RETIRE")(doc)["reason"]


def _added_without_pack(doc):
    doc["icons"]["pack"] = None


def _pack_with_empty_added(doc):
    doc["icons"]["added"] = []


def _alias_row_missing_commit(doc):
    doc["ops"].append({"op": "UPSERT", "entity": "uid_alias", "key": "1000004", "rev": 2,
                       "data": {"uid_alias": {"old_uid": "1000004", "new_uid": "1000005", "since_revision": 2}}})


def _manifest_snapshot_only_with_updates(doc):
    doc["snapshot_only"] = True
    doc["snapshot_only_reasons"] = ["forced"]


def _manifest_not_snapshot_only_with_reasons(doc):
    doc["snapshot_only_reasons"] = ["forced"]


def _manifest_not_snapshot_only_without_updates(doc):
    doc["artifacts"]["updates"] = []


def _manifest_snapshot_only_without_reason(doc):
    doc["snapshot_only"] = True
    doc["snapshot_only_reasons"] = []
    doc["artifacts"]["updates"] = []
    doc["artifacts"]["icon_packs"]["deltas"] = []


def _manifest_drop_table(doc):
    del doc["entity_counts"]["item"]


def _manifest_extra_table(doc):
    doc["entity_counts"]["ledger"] = 0


MUTATIONS = [
    ("current", "missing-sig", _del(["sig"])),
    ("current", "short-sig-87", _set(["sig"], "A" * 85 + "==")),
    ("current", "sig-without-padding", _set(["sig"], "A" * 88)),
    ("current", "offset-timestamp", _set(["published_at"], "2026-10-04T23:21:04+00:00")),
    ("current", "fractional-seconds", _set(["checked_at"], "2026-10-04T23:21:04.5Z")),
    ("current", "manifest-leading-zero", _set(["manifest"], "manifests/01.json")),
    ("current", "extra-property", _set(["extra"], 1)),
    ("current", "revision-zero", _set(["revision"], 0)),
    ("current", "revision-as-string", _set(["revision"], "2")),
    ("current", "revision-16-digits", _set(["revision"], 10**15)),
    ("current", "uppercase-sha", _set(["manifest_sha256"], "A" * 64)),
    ("current", "missing-api-schema-version", _del(["api_schema_version"])),
    ("current", "minimum-revision-zero", _set(["minimum_revision"], 0)),
    ("manifest", "snapshot-only-with-updates", _manifest_snapshot_only_with_updates),
    ("manifest", "not-snapshot-only-with-reasons", _manifest_not_snapshot_only_with_reasons),
    ("manifest", "not-snapshot-only-without-updates", _manifest_not_snapshot_only_without_updates),
    ("manifest", "snapshot-only-without-reason", _manifest_snapshot_only_without_reason),
    ("manifest", "missing-count-table", _manifest_drop_table),
    ("manifest", "unknown-count-table", _manifest_extra_table),
    ("manifest", "negative-count", _set(["entity_counts", "equipment"], -1)),
    ("manifest", "update-path-not-numeric", _set(["artifacts", "updates", 0, "path"], "updates/1-x.json.gz")),
    ("manifest", "gate-level-error", _set(["gate", "level"], "error")),
    ("manifest", "short-build-commit", _set(["build", "build_commit"], "abc123")),
    ("manifest", "missing-runtime", _del(["build", "runtime"])),
    ("manifest", "zero-byte-snapshot", _set(["artifacts", "snapshot", "bytes"], 0)),
    ("manifest", "base-pack-wrong-extension", _set(["artifacts", "icon_packs", "base", "path"], "icons/packs/base-2")),
    ("update_package", "equipment-without-stat", _drop_stat),
    ("update_package", "weapon-and-monster", _weapon_and_monster),
    ("update_package", "nested-object-in-row", _nested_row_value),
    ("update_package", "retire-on-item", _retire_item),
    ("update_package", "alias-without-to", _alias_missing("to")),
    ("update_package", "alias-without-confirmed-commit", _alias_missing("confirmed_commit")),
    ("update_package", "delete-equipment", _append({"op": "DELETE", "entity": "equipment", "key": "1000001", "rev": 2})),
    ("update_package", "delete-uid-retired", _append({"op": "DELETE", "entity": "uid_retired", "key": "1000003", "rev": 2})),
    ("update_package", "negative-hash-key", _append({"op": "UPSERT", "entity": "skill_effect", "key": "-77", "rev": 2, "data": {"skill_effect": {"effect_id": "-77"}}})),
    ("update_package", "non-numeric-wiki-uid", _append({"op": "UPSERT", "entity": "item", "key": "abc", "rev": 2, "data": {"item": {"uid": "abc"}}})),
    ("update_package", "op-insert", _append({"op": "INSERT", "entity": "item", "key": "1", "rev": 2, "data": {"item": {"uid": "1"}}})),
    ("update_package", "added-with-null-pack", _added_without_pack),
    ("update_package", "pack-with-empty-added", _pack_with_empty_added),
    ("update_package", "ledger-block-present", _set(["ledger"], {"uid_retired": [], "uid_alias": []})),
    ("update_package", "pack-bytes-present", _set(["icons", "pack_bytes"], 10)),
    ("update_package", "retire-without-reason", _retire_without_reason),
    ("update_package", "uid-alias-without-confirmed-commit", _alias_row_missing_commit),
    ("update_package", "missing-icons", _del(["icons"])),
    ("update_package", "revision-as-string", _set(["to_revision"], "2")),
    ("requires_snapshot", "flag-false", _set(["requires_snapshot"], False)),
    ("requires_snapshot", "missing-snapshot-revision", _del(["snapshot_revision"])),
    ("requires_snapshot", "reason-other", _set(["reason"], "other")),
    ("requires_snapshot", "extra-property", _set(["note"], "x")),
    ("requires_snapshot", "snapshot-revision-zero", _set(["snapshot_revision"], 0)),
]

ICON_PACK_MUTATIONS = [
    ("base", "base-with-from-revision", _set(["from_revision"], 1)),
    ("base", "base-named-delta", _set(["name"], "delta-1-2.zip")),
    ("base", "kind-packs", _set(["icons", 0, "kind"], "packs")),
    ("base", "kind-skill", _set(["icons", 0, "kind"], "skill")),
    ("base", "path-not-kind-sha", _set(["icons", 0, "path"], "equipment/abc.png")),
    ("base", "name-without-zip", _set(["name"], "base-2")),
    ("delta", "delta-with-null-from", _set(["from_revision"], None)),
    ("delta", "delta-without-icons", _set(["icons"], [])),
    ("delta", "pack-type-other", _set(["pack_type"], "full")),
]

ERROR_MUTATIONS = [
    ("code-teapot", {"error": "teapot", "message": "x"}),
    ("missing-message", {"error": "not_found"}),
    ("message-too-long", {"error": "not_found", "message": "x" * 201}),
    ("extra-property", {"error": "not_found", "message": "x", "detail": 1}),
]


@pytest.mark.parametrize(("name", "label", "mutate"), MUTATIONS, ids=[f"{n}-{label}" for n, label, _ in MUTATIONS])
def test_broken_instance_is_rejected(contract, docs, name, label, mutate):
    broken = copy.deepcopy(docs[name])
    mutate(broken)
    assert broken != docs[name], "the mutation must change the instance"
    assert contract.errors(name, broken), f"{name} accepted a broken instance ({label})"


@pytest.mark.parametrize(("base", "label", "mutate"), ICON_PACK_MUTATIONS, ids=[f"icon_pack-{label}" for _, label, _ in ICON_PACK_MUTATIONS])
def test_broken_icon_pack_index_is_rejected(contract, docs, base, label, mutate):
    broken = copy.deepcopy(docs[base])
    mutate(broken)
    assert broken != docs[base]
    assert contract.errors("icon_pack", broken), f"icon_pack accepted a broken index ({label})"


@pytest.mark.parametrize(("label", "instance"), ERROR_MUTATIONS, ids=[f"error-{label}" for label, _ in ERROR_MUTATIONS])
def test_broken_error_body_is_rejected(contract, label, instance):
    assert contract.error_errors(instance)


def test_at_least_thirty_broken_instances_are_exercised():
    assert len(MUTATIONS) + len(ICON_PACK_MUTATIONS) + len(ERROR_MUTATIONS) >= 30


def test_mutation_labels_are_unique():
    labels = [(n, label) for n, label, _ in MUTATIONS]
    assert len(labels) == len(set(labels))


def test_regex_patterns_in_schemas_compile(contract):
    for name, schema in contract.schemas.items():
        for ref in re.findall(r'"pattern":\s*"((?:[^"\\]|\\.)*)"', json.dumps(schema)):
            re.compile(json.loads(f'"{ref}"'))


def test_contract_directory_layout():
    contract_dir = SCHEMAS.parent
    assert (contract_dir / "openapi.yaml").is_file()
    assert Path(contract_dir / "schemas").is_dir()
