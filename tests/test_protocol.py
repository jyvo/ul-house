"""The protocol mirrors the API contract"""
import dataclasses
import json
import re
import typing
from pathlib import Path

import pytest

import pub_fixture as pf
from ul_house import ids
from ul_house.database import entities
from ul_house.sync import protocol, verify
from ul_house.sync.errors import ProtocolError

SCHEMA_FILES = ("current", "manifest", "update_package", "icon_pack", "requires_snapshot")

needs_contract = pytest.mark.skipif(not pf.contract_available(),
                                    reason="api/contract/schemas/ not present yet (T4); must run, not skip, in the validation round")


def schema(name: str) -> dict:
    return json.loads((pf.SCHEMAS / f"{name}.schema.json").read_text())


def pointer(doc, path: str):
    node = doc
    for part in [p for p in path.split("/") if p]:
        node = node[part]
    return node


def resolve(node: dict, root: dict) -> dict:
    """follow local $ref chains; nullable collapses"""
    seen = 0
    while "$ref" in node:
        node = {**pointer(root, node["$ref"].lstrip("#")), **{k: v for k, v in node.items() if k != "$ref"}}
        seen += 1
        assert seen < 10
    if "oneOf" in node:
        options = node["oneOf"]
        non_null = [o for o in options if o.get("type") != "null"]
        if len(non_null) == 1 and len(options) == 2:
            return {**resolve(non_null[0], root), "nullable": True}
    return node


def json_types(node: dict) -> set[str]:
    t = node.get("type")
    if t is None:
        names = {bool: "boolean", int: "integer", float: "number", str: "string", type(None): "null"}
        if "const" in node:
            return {names[type(node["const"])]}
        if "enum" in node:
            return {names[type(v)] for v in node["enum"]}
        return {"any"}
    return set(t) if isinstance(t, list) else {t}


# (dataclass, schema file, JSON pointer)
CASES = [
    (protocol.Current, "current", ""),
    (protocol.RequiresSnapshot, "requires_snapshot", ""),
    (protocol.Manifest, "manifest", ""),
    (protocol.Freshness, "manifest", "/properties/freshness"),
    (protocol.Build, "manifest", "/properties/build"),
    (protocol.Runtime, "manifest", "/properties/build/properties/runtime"),
    (protocol.Artifacts, "manifest", "/properties/artifacts"),
    (protocol.SnapshotRef, "manifest", "/$defs/snapshot_ref"),
    (protocol.UpdateRef, "manifest", "/$defs/update_ref"),
    (protocol.IconPacks, "manifest", "/properties/artifacts/properties/icon_packs"),
    (protocol.BasePackRef, "manifest", "/$defs/base_pack_ref"),
    (protocol.DeltaPackRef, "manifest", "/$defs/delta_pack_ref"),
    (protocol.LineageRef, "manifest", "/$defs/lineage_ref"),
    (protocol.ManifestIcons, "manifest", "/properties/icons"),
    (protocol.Gate, "manifest", "/properties/gate"),
    (protocol.GateFinding, "manifest", "/$defs/gate_finding"),
    (protocol.UpdatePackage, "update_package", ""),
    (protocol.UpsertOp, "update_package", "/$defs/upsert"),
    (protocol.RetireOp, "update_package", "/$defs/retire"),
    (protocol.AliasOp, "update_package", "/$defs/alias"),
    (protocol.DeleteOp, "update_package", "/$defs/delete"),
    (protocol.IconDelta, "update_package", "/$defs/icons"),
    (protocol.IconPackListing, "icon_pack", ""),
    (protocol.IconPackEntry, "icon_pack", "/properties/icons/items"),
]
CASE_IDS = [f"{cls.__name__}<-{name}{ptr}" for cls, name, ptr in CASES]


def annotation_kind(annotation) -> tuple[str, bool]:
    """kind and optionality of a hint"""
    origin = typing.get_origin(annotation)
    optional = False
    if origin in (typing.Union, type(int | None)):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        optional = len(members) != len(typing.get_args(annotation))
        assert len(members) == 1, annotation
        annotation = members[0]
        origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return "literal", optional
    if origin is tuple:
        return "array", optional
    if origin in (dict,) or (origin is not None and getattr(origin, "__name__", "") == "Mapping"):
        return "object", optional
    if dataclasses.is_dataclass(annotation):
        return "object", optional
    return {str: "string", int: "integer", float: "number", bool: "boolean"}[annotation], optional


# version agreement
def test_api_schema_version_is_the_major_of_the_contract_version():
    yaml = pytest.importorskip("yaml")
    openapi = pf.CONTRACT / "openapi.yaml"
    if not openapi.exists():
        pytest.skip("api/contract/openapi.yaml not present yet (T4)")
    version = str(yaml.safe_load(openapi.read_text())["info"]["version"])
    assert protocol.API_SCHEMA_VERSION == int(version.split(".")[0])
    assert protocol.API_SCHEMA_VERSION == 1


def test_dataset_schema_version_comes_from_the_registry():
    assert entities.DATASET_SCHEMA_VERSION == 1
    sections = entities.schema_sections()
    assert "dataset_schema_version: 1" in entities.SCHEMA_PATH.read_text()
    assert set(sections) == {"game", "ledger", "sync", "user"}


@needs_contract
@pytest.mark.parametrize("cls,name,ptr", CASES, ids=CASE_IDS)
def test_dataclass_mirrors_its_schema(cls, name, ptr):
    root = schema(name)
    node = resolve(pointer(root, ptr), root) if ptr else root
    props = node["properties"]
    hints = typing.get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    assert set(fields) == set(props), f"{cls.__name__} fields vs schema properties"
    required = set(node.get("required", ()))
    without_default = {n for n, f in fields.items()
                       if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    literal_tag = {n for n in fields if n == "op"}
    assert without_default | literal_tag >= required
    assert required >= without_default, f"{cls.__name__}: fields without a default that the schema does not require"
    assert node.get("additionalProperties") is False
    for fname, field in fields.items():
        prop = resolve(props[fname], root)
        kind, optional = annotation_kind(hints[fname])
        types = json_types(prop)
        if kind == "literal":
            options = set(typing.get_args(hints[fname]))
            assert options == ({prop["const"]} if "const" in prop else set(prop["enum"])), fname
            continue
        assert kind in types or (kind == "number" and "integer" in types) or (kind == "object" and "any" in types), \
            f"{cls.__name__}.{fname}: {kind} vs schema {types}"
        if optional and fname in required:
            assert prop.get("nullable") or "null" in types, f"{cls.__name__}.{fname}: Optional but the schema forbids null"
        if prop.get("nullable") or "null" in types:
            assert optional, f"{cls.__name__}.{fname}: the schema allows null, the dataclass does not"


@needs_contract
def test_every_schema_file_is_mirrored():
    on_disk = {p.name.removesuffix(".schema.json") for p in pf.SCHEMAS.glob("*.schema.json")}
    assert on_disk == set(SCHEMA_FILES)
    assert {name for _cls, name, _ptr in CASES} == on_disk


@needs_contract
def test_op_classes_match_the_oneof_branches():
    root = schema("update_package")
    branches = {pointer(root, b["$ref"].lstrip("#"))["properties"]["op"]["const"]: b["$ref"]
                for b in root["$defs"]["op"]["oneOf"]}
    assert set(branches) == set(protocol.OP_TYPES) == {"UPSERT", "RETIRE", "ALIAS", "DELETE"}
    for op, cls in protocol.OP_TYPES.items():
        assert typing.get_args(typing.get_type_hints(cls)["op"]) == (op,)
        assert branches[op].endswith(cls.__name__.removesuffix("Op").lower()) or cls.__name__ == "UpsertOp"


@needs_contract
def test_entity_enum_is_the_registry():
    root = schema("update_package")
    registry = {e.entity_type for e in entities.ENTITIES}
    assert len(registry) == 11
    assert set(root["$defs"]["entity"]["enum"]) == registry == set(protocol.ENTITY_TYPES)
    assert set(root["$defs"]["deletable_entity"]["enum"]) == {e.entity_type for e in entities.ENTITIES if e.deletable}
    assert set(protocol.DELETABLE_ENTITIES) == {e.entity_type for e in entities.ENTITIES if e.deletable}
    assert {e.entity_type for e in entities.ENTITIES if e.retirable} == {"equipment"} == \
        {e.entity_type for e in entities.ENTITIES if e.aliasable}
    assert root["$defs"]["retire"]["properties"]["entity"]["const"] == "equipment"
    assert root["$defs"]["alias"]["properties"]["entity"]["const"] == "equipment"
    assert not {"equipment", "uid_alias", "uid_retired"} & set(root["$defs"]["deletable_entity"]["enum"])


@needs_contract
def test_key_patterns_equal_the_protocol_constants():
    defs = schema("update_package")["$defs"]
    assert defs["wiki_uid"]["pattern"] == protocol.KEY_PATTERNS["wiki_uid"]
    assert defs["hash_id"]["pattern"] == protocol.KEY_PATTERNS["hash_id"]
    assert defs["sha256"]["pattern"] == protocol.KEY_PATTERNS["sha256"]
    assert defs["element_id"]["pattern"] == protocol.KEY_PATTERNS["element_id"]
    for name in ("current", "manifest", "icon_pack"):
        assert schema(name)["$defs"]["sha256"]["pattern"] == protocol.KEY_PATTERNS["sha256"]


@needs_contract
def test_icon_kinds_are_one_vocabulary():
    kinds = set(protocol.ICON_KINDS)
    assert kinds == {"equipment", "item", "ability"}
    entry = schema("icon_pack")["properties"]["icons"]["items"]["properties"]
    assert set(entry["kind"]["enum"]) == kinds
    assert entry["path"]["pattern"].startswith("^(" + "|".join(protocol.ICON_KINDS) + ")/")
    assert f"CHECK (kind IN ({', '.join(repr(k) for k in protocol.ICON_KINDS)}))" in re.sub(
        r"\s+", " ", entities.schema_sections()["game"])
    yaml = pytest.importorskip("yaml")
    openapi = pf.CONTRACT / "openapi.yaml"
    if openapi.exists():
        assert set(yaml.safe_load(openapi.read_text())["components"]["parameters"]["IconKind"]["schema"]["enum"]) == kinds


@needs_contract
def test_api_error_mirrors_openapi():
    yaml = pytest.importorskip("yaml")
    openapi = pf.CONTRACT / "openapi.yaml"
    if not openapi.exists():
        pytest.skip("openapi.yaml not present yet")
    error = yaml.safe_load(openapi.read_text())["components"]["schemas"]["Error"]
    assert set(dataclasses.fields(protocol.ApiError)[i].name for i in range(2)) == set(error["properties"]) == set(error["required"])
    assert protocol.ApiError.from_json({"error": "no_update", "message": "x"}).error == "no_update"


@needs_contract
def test_manifest_snapshot_reasons_and_enums_equal_the_schema():
    manifest = schema("manifest")
    assert set(manifest["properties"]["snapshot_only_reasons"]["items"]["enum"]) == set(protocol.SNAPSHOT_ONLY_REASONS)
    assert set(schema("requires_snapshot")["properties"]["reason"]["enum"]) == set(protocol.REQUIRES_SNAPSHOT_REASONS)
    assert set(schema("update_package")["$defs"]["retire_reason"]["enum"]) == set(protocol.RETIRE_REASONS)
    assert set(manifest["properties"]["entity_counts"]["required"]) == set(pf.GAME_TABLES)


# entities.py agreement
@needs_contract
@pytest.mark.parametrize("entity", entities.ENTITIES, ids=lambda e: e.entity_type)
def test_aggregate_definition_matches_the_registry(entity):
    defs = schema("update_package")["$defs"]
    aggregate = defs[f"aggregate_{entity.entity_type}"]
    expected = {entity.root_table} | {c.table for c in entity.children}
    assert set(aggregate["properties"]) == expected, f"{entity.entity_type}: tables in the package vs in entities.py"
    assert aggregate["additionalProperties"] is False
    optional = {c.table for c in entity.children if c.table in pf.ONE_TO_ONE}
    assert set(aggregate["required"]) == expected - optional
    for table in optional:
        assert aggregate["properties"][table]["$ref"].endswith("/row")
    for table in expected - optional - {entity.root_table}:
        assert aggregate["properties"][table]["$ref"].endswith("/rows")
    root = aggregate["properties"][entity.root_table]
    assert root.get("$ref", "").endswith("/row") or root.get("type") == "object"


@needs_contract
@pytest.mark.parametrize("entity", entities.ENTITIES, ids=lambda e: e.entity_type)
def test_key_pattern_matches_the_entity_key_type(entity):
    from jsonschema import Draft202012Validator

    defs = schema("update_package")["$defs"]
    validator = Draft202012Validator({"$defs": defs, "$ref": "#/$defs/key_matches_entity"})
    good = {"equipment": "1001", "item": "7001", "weapon_ability": "5001", "uid_retired": "2001", "uid_alias": "2001",
            "icon": "ab" * 32, "element": "fire"}.get(entity.entity_type)
    if entity.key_type == "int":
        good = ids.id_to_text(ids.hash_id(entity.entity_type))
        bad = "not-a-number"
    else:
        bad = {"icon": "1001", "element": "1001"}.get(entity.entity_type, "fire")
    validator.validate({"entity": entity.entity_type, "key": good})
    assert not validator.is_valid({"entity": entity.entity_type, "key": bad})
    assert protocol.ENTITY_KEY_PATTERN[entity.entity_type] in protocol.KEY_PATTERNS


@needs_contract
def test_every_aggregate_payload_of_the_fixture_validates_row_by_row():
    """fixture rows match schema.sql columns"""
    for r, n in ((1, 2), (1, 3), (1, 4), (2, 3), (3, 4)):
        pf.validate_doc("update_package", pf.make_package(r, n))


# fixtures validate against contract
@needs_contract
def test_every_fixture_artifact_validates_against_the_schemas(tmp_path):
    pub = pf.build_pub(tmp_path, validate=False)
    pf.validate_pub(pub)
    from jsonschema import Draft202012Validator

    rs = Draft202012Validator(schema("requires_snapshot"))
    for reason in ("below_minimum", "package_missing"):
        rs.validate(json.loads(pf.requires_snapshot_reply(4, 1, reason).body))
    only = pf.build_pub(tmp_path / "only", snapshot_only=True)
    pf.validate_doc("manifest", only.manifest())
    assert only.manifest()["artifacts"]["updates"] == [] and only.minimum_revision == 4


@needs_contract
def test_fixture_manifest_parses_and_round_trips(tmp_path):
    pub = pf.build_pub(tmp_path)
    manifest = protocol.Manifest.from_json(pub.read("manifests/4.json"))
    assert manifest.revision == 4 and manifest.update_from(1).path == "updates/1-4.json.gz"
    assert manifest.update_from(4) is None and manifest.delta_pack(1).icon_count == 2
    assert manifest.to_json() == pub.manifest()
    package = protocol.UpdatePackage.from_json(pub.package(1))
    assert package.to_json() == pub.package(1)


@needs_contract
@pytest.mark.parametrize("name,cls", [("current", protocol.Current), ("requires_snapshot", protocol.RequiresSnapshot),
                                      ("manifest", protocol.Manifest), ("update_package", protocol.UpdatePackage),
                                      ("icon_pack", protocol.IconPackListing)])
def test_schema_examples_round_trip(name, cls):
    examples = schema(name).get("examples")
    if not examples:
        pytest.skip(f"{name}.schema.json carries no examples")
    for example in examples:
        assert cls.from_json(example).to_json() == example


@pytest.fixture
def doc():
    return pf.sign_current(pf.sample_current_doc(), pf.KEY)


def test_a_valid_document_round_trips(doc):
    current = protocol.Current.from_json(doc)
    assert current.to_json() == doc
    assert protocol.Current.from_json(json.dumps(doc)).to_json() == doc
    assert protocol.Current.from_json(json.dumps(doc).encode()).revision == 4


@pytest.mark.parametrize("field,value", [
    ("revision", True), ("revision", "4"), ("revision", 4.5), ("revision", 0), ("revision", -1),
    ("minimum_revision", None), ("dataset_schema_version", False), ("api_schema_version", 0),
    ("manifest_sha256", "ab" * 31), ("manifest_sha256", "AB" * 32), ("manifest_sha256", "g" * 64),
    ("published_at", "2026-10-01"), ("checked_at", "2026-10-04T00:00:00+00:00"), ("expires_at", "2026-10-18 00:00:00Z"),
    ("manifest", "../manifests/4.json"), ("manifest", "manifests/04.json"), ("sig", "short"), ("sig", 7),
])
def test_bad_values_are_protocol_errors(doc, field, value):
    doc[field] = value
    with pytest.raises(ProtocolError):
        protocol.Current.from_json(doc)


def test_missing_required_field_is_a_protocol_error(doc):
    for field in list(doc):
        broken = {k: v for k, v in doc.items() if k != field}
        with pytest.raises(ProtocolError):
            protocol.Current.from_json(broken)


def test_unknown_fields_are_ignored(doc):
    doc["something_new"] = {"nested": [1, 2, 3]}
    current = protocol.Current.from_json(doc)
    assert current.revision == 4 and "something_new" not in current.to_json()


def test_not_an_object_is_a_protocol_error():
    for bad in ([], "x", 3, None):
        with pytest.raises(ProtocolError):
            protocol.Current.from_json(bad)


def test_loads_rejects_duplicates_nan_and_non_objects():
    assert protocol.loads(b'{"a": 1}') == {"a": 1}
    for raw in (b'{"a": 1, "a": 2}', b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', b"[1]", b'"s"', b"nope",
                b"", b'{"a": 1} trailing'):
        with pytest.raises(ProtocolError):
            protocol.loads(raw)
    assert protocol.loads('{"café": "☃"}') == {"café": "☃"}


def package_doc(**changes):
    doc = pf.make_package(1, 2)
    doc.update(changes)
    return doc


def test_a_package_parses_into_typed_ops():
    package = protocol.UpdatePackage.from_json(package_doc())
    kinds = [type(op) for op in package.ops]
    assert protocol.UpsertOp in kinds and protocol.RetireOp in kinds
    assert package.from_revision == 1 and package.to_revision == 2 and package.icons.pack == "icons/packs/delta-1-2.zip"
    retire = next(op for op in package.ops if isinstance(op, protocol.RetireOp))
    assert (retire.entity, retire.key, retire.rev, retire.reason) == ("equipment", "2001", 2, "gone")
    alias = next(op for op in protocol.UpdatePackage.from_json(pf.make_package(2, 3)).ops
                 if isinstance(op, protocol.AliasOp))
    assert (alias.key, alias.to, alias.confirmed_commit) == ("2001", "2002", pf.COMMIT)


def test_an_unknown_op_is_a_protocol_error():
    doc = package_doc()
    doc["ops"].append({"op": "MERGE", "entity": "equipment", "key": "1", "rev": 2})
    with pytest.raises(ProtocolError):
        protocol.UpdatePackage.from_json(doc)
    doc["ops"][-1] = {"entity": "equipment", "key": "1", "rev": 2}
    with pytest.raises(ProtocolError):
        protocol.UpdatePackage.from_json(doc)


def test_ops_are_validated_against_the_registry():
    for bad in (
        {"op": "DELETE", "entity": "equipment", "key": "1001", "rev": 2},
        {"op": "DELETE", "entity": "uid_alias", "key": "2001", "rev": 2},
        {"op": "RETIRE", "entity": "item", "key": "7001", "rev": 2, "reason": "gone"},
        {"op": "RETIRE", "entity": "equipment", "key": "2001", "rev": 2, "reason": "because"},
        {"op": "ALIAS", "entity": "item", "key": "1", "to": "2", "rev": 2, "confirmed_commit": pf.COMMIT},
        {"op": "ALIAS", "entity": "equipment", "key": "1", "to": "2", "rev": 2, "confirmed_commit": "abc"},
        {"op": "UPSERT", "entity": "nonsense", "key": "1", "rev": 2, "data": {"item": {"uid": "1"}}},
        {"op": "UPSERT", "entity": "equipment", "key": "not-a-uid", "rev": 2, "data": {"equipment": {"uid": "1"}}},
        {"op": "UPSERT", "entity": "proc", "key": "007", "rev": 2, "data": {"proc": {"proc_id": 7}}},
        {"op": "UPSERT", "entity": "icon", "key": "XYZ", "rev": 2, "data": {"icon": {"sha256": "x"}}},
    ):
        doc = package_doc()
        doc["ops"] = [bad]
        with pytest.raises(ProtocolError):
            protocol.UpdatePackage.from_json(doc)


def test_row_values_must_be_scalars():
    base = {"op": "UPSERT", "entity": "item", "key": "7001", "rev": 2}
    for bad_row in ({"uid": "7001", "name": {"nested": 1}}, {"uid": "7001", "name": ["a"]}, {"uid": "7001", "name": True},
                    {}, {"Bad-Column": 1}):
        doc = package_doc()
        doc["ops"] = [{**base, "data": {"item": bad_row}}]
        with pytest.raises(ProtocolError):
            protocol.UpdatePackage.from_json(doc)
    doc = package_doc()
    doc["ops"] = [{**base, "data": {"item": {"uid": "7001", "name": "Iron"}}}]
    assert protocol.UpdatePackage.from_json(doc).ops[0].key == "7001"
    doc["ops"] = [{**base, "data": {"item": {"uid": "7001", "name": 1.5}}}]
    protocol.UpdatePackage.from_json(doc)


def test_icon_delta_is_null_iff_nothing_was_added():
    ok = {"pack": None, "pack_sha256": None, "added": []}
    assert protocol.IconDelta.from_json(ok).added == ()
    for bad in ({"pack": "icons/packs/delta-1-2.zip", "pack_sha256": "ab" * 32, "added": []},
                {"pack": None, "pack_sha256": None, "added": ["ab" * 32]},
                {"pack": "icons/packs/delta-1-2.zip", "pack_sha256": None, "added": ["ab" * 32]},
                {"pack": "icons/packs/delta-1-2.zip", "pack_sha256": "ab" * 32, "added": ["nothex"]},
                {"pack": "../../etc/passwd", "pack_sha256": "ab" * 32, "added": ["ab" * 32]}):
        with pytest.raises(ProtocolError):
            protocol.IconDelta.from_json(bad)
    assert "pack_bytes" not in {f.name for f in dataclasses.fields(protocol.IconDelta)}


def test_requires_snapshot_parses_both_reasons():
    for reason in ("below_minimum", "package_missing"):
        parsed = protocol.RequiresSnapshot.from_json(json.loads(pf.requires_snapshot_reply(4, 0, reason).body))
        assert parsed.reason == reason and parsed.snapshot_revision == 4 and parsed.from_revision == 0
    with pytest.raises(ProtocolError):
        protocol.RequiresSnapshot.from_json({"requires_snapshot": False, "snapshot_revision": 4, "from_revision": 1,
                                             "reason": "below_minimum"})
    with pytest.raises(ProtocolError):
        protocol.RequiresSnapshot.from_json({"requires_snapshot": True, "snapshot_revision": 4, "from_revision": 1,
                                             "reason": "whim"})


def test_canonical_form_is_shared_with_the_signer():
    """sign.py imports verify.canonical_bytes; no second copy"""
    assert not hasattr(protocol, "canonical_bytes")
    assert verify.canonical_bytes({"b": 1, "a": "é"}) == b'{"a":"\\u00e9","b":1}'


def test_manifest_helpers(tmp_path):
    pub = pf.build_pub(tmp_path)
    manifest = protocol.Manifest.from_json(pub.read("manifests/4.json"))
    assert [u.from_revision for u in manifest.artifacts.updates] == [1, 2, 3]
    assert manifest.delta_pack(3) is None and manifest.delta_pack(2).path == "icons/packs/delta-2-4.zip"
    assert manifest.artifacts.icon_packs.base.icon_count == 7
    assert set(manifest.entity_counts) == set(pf.GAME_TABLES)
    assert manifest.snapshot_only is False and manifest.gate.level == "pass"
