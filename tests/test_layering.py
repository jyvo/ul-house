"""import and write boundaries
    - crawl/ + http/ never import parse, extract or models
    - build/ never imports http/
    - only http/ imports requests
    - seed/ imports neither crawl/ nor http/
    - app/ and sync/ import nothing from crawl/, seed/, extract, parse, select or build/
    - sync/ is the only code that writes game tables
    - app/ and sync/ never reference config.BASE_URL
    - api/ imports nothing from ul_house
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "ul_house"
SCHEMA = PACKAGE / "database" / "schema.sql"

DML_RE = re.compile(r"""\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+(?:\w+\.)?["`\[]?(\w+)""", re.I)
REPOINT_RE = re.compile(r"""\bUPDATE\s+(?:\w+\.)?["`\[]?(inventory|saved_ref)["`\]]?\s+SET\s+uid\s*=\s*[^,]*?(?:\bWHERE\b|\bFROM\b|$)""", re.I | re.S)
SAVED_REF_DELETE_RE = re.compile(r"""\bDELETE\s+FROM\s+(?:\w+\.)?["`\[]?saved_ref["`\]]?\s+WHERE\b""", re.I)
SECTION_RE = re.compile(r"^-- @section (game|ledger|sync|user)$", re.M)
CREATE_TABLE_RE = re.compile(r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I | re.M)


def package_of(path: Path) -> list[str] | None:
    parts = path.parts
    if "ul_house" not in parts:
        return None
    start = len(parts) - 1 - parts[::-1].index("ul_house")
    return list(parts[start:-1])


def resolve(node: ast.ImportFrom, package: list[str] | None) -> str | None:
    if node.level == 0:
        return node.module
    if package is None or node.level > len(package):
        return None
    base = package[: len(package) - node.level + 1]
    return ".".join(base + ([node.module] if node.module else []))


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    package = package_of(path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve(node, package)
            if not module:
                continue
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def modules(subdir: str = "") -> list[Path]:
    root = PACKAGE / subdir if subdir else PACKAGE
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def label(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def offenders(files: list[Path], banned: tuple[str, ...]) -> dict[str, list[str]]:
    found = {}
    for path in files:
        hits = sorted(name for name in imports_of(path) if name.startswith(banned))
        if hits:
            found[label(path)] = hits
    return found


@pytest.mark.parametrize("subdir", ["crawl", "http"])
def test_crawler_never_parses(subdir):
    assert offenders(modules(subdir), ("ul_house.parse", "ul_house.extract", "ul_house.models")) == {}


def test_build_never_touches_the_network():
    assert offenders(modules("build"), ("ul_house.http", "requests")) == {}


def test_only_http_imports_requests():
    outside = [p for p in modules() if "http" not in p.relative_to(PACKAGE).parts]
    assert offenders(outside, ("requests",)) == {}


def test_store_neither_decides_nor_fetches():
    assert offenders(modules("seed"), ("ul_house.crawl", "ul_house.http", "ul_house.select")) == {}


@pytest.mark.parametrize("name", ["config.py", "settings.py"])
def test_configuration_is_inert(name):
    assert offenders([PACKAGE / name], ("ul_house.http", "ul_house.crawl", "ul_house.seed", "requests")) == {}


def test_the_scan_sees_imports():
    """guard against a scanner that silently finds nothing"""
    assert "ul_house.extract" in imports_of(PACKAGE / "parse.py")


# ensure installer doesnt ship crawler nor transformer
@pytest.mark.parametrize("subdir", ["app", "sync"])
def test_installer_ships_neither_crawler_nor_transform(subdir):
    banned = ("ul_house.crawl", "ul_house.seed", "ul_house.extract", "ul_house.parse", "ul_house.select", "ul_house.build")
    assert offenders(modules(subdir), banned) == {}


def test_relative_imports_resolved(tmp_path):
    fake = tmp_path / "ul_house" / "sync" / "apply.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("from ..crawl import x\nfrom . import protocol\nfrom .. import config\n")
    names = imports_of(fake)
    assert {"ul_house.crawl", "ul_house.crawl.x", "ul_house.sync.protocol", "ul_house.config"} <= names
    assert offenders([fake], ("ul_house.crawl",)) == {str(fake): ["ul_house.crawl", "ul_house.crawl.x"]}


def test_relative_import_in_package_init(tmp_path):
    fake = tmp_path / "ul_house" / "app" / "__init__.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("from ..build import cli\n")
    assert "ul_house.build.cli" in imports_of(fake)


# table write rules
def schema_tables(text: str | None = None) -> dict[str, set[str]]:
    """{section: CREATE TABLE names} parsed from database/schema.sql"""
    text = SCHEMA.read_text() if text is None else text
    parts = SECTION_RE.split(text)
    return {name: set(CREATE_TABLE_RE.findall(body)) for name, body in zip(parts[1::2], parts[2::2])}


def docstring_nodes(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def sql_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    skip = docstring_nodes(tree)
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def dml_targets(text: str) -> set[str]:
    return {match.group(1).lower() for match in DML_RE.finditer(text)}


def is_repoint(text: str, start: int) -> bool:
    return REPOINT_RE.match(text, start) is not None or SAVED_REF_DELETE_RE.match(text, start) is not None


def writes(path: Path, tables: set[str], allow_repoint: bool = False) -> list[str]:
    hits = set()
    for text in sql_strings(path):
        for match in DML_RE.finditer(text):
            table = match.group(1).lower()
            if table not in tables:
                continue
            if allow_repoint and is_repoint(text, match.start()):
                continue
            hits.add(table)
    return sorted(hits)


def scanned_modules() -> list[Path]:
    """every module except build/ (CI-only)"""
    return [p for p in modules() if p.relative_to(PACKAGE).parts[0] != "build"]


def test_only_sync_writes_game_tables():
    sections = schema_tables()
    shipped = sections["game"] | sections["ledger"]
    found = {}
    for path in scanned_modules():
        if path.relative_to(PACKAGE).parts[0] == "sync":
            continue
        if hits := writes(path, shipped):
            found[label(path)] = hits
    assert found == {}


def test_only_app_writes_user_tables():
    user = schema_tables()["user"]
    found = {}
    for path in scanned_modules():
        rel = path.relative_to(PACKAGE).parts
        if rel[0] == "app":
            continue
        if hits := writes(path, user, allow_repoint=rel == ("sync", "apply.py")):
            found[label(path)] = hits
    assert found == {}


def test_schema_table_sets_parsed():
    sections = schema_tables()
    assert list(sections) == ["game", "ledger", "sync", "user"]
    assert sections["game"] == {
        "element", "element_relation", "item", "icon", "skill_effect", "proc_family", "proc",
        "proc_condition", "proc_scaling", "weapon_ability", "passive_skill", "equipment", "weapon",
        "defensive_gear", "monster", "monster_skill", "potential_level", "effect_link", "stat",
        "evolution_edge", "evolution_chain", "evolution_material",
    }
    assert sections["ledger"] == {"uid_retired", "uid_alias"}
    assert sections["sync"] == {"sync_history", "sync_state"}
    assert sections["user"] == {"profile", "inventory", "saved_ref", "gear_set", "gear_set_slot"}


@pytest.mark.parametrize("text, expected", [
    ("INSERT INTO equipment (uid) VALUES (?)", {"equipment"}),
    ("insert or replace into stat VALUES (?)", {"stat"}),
    ("DELETE FROM main.inventory WHERE uid = ?", {"inventory"}),
    ('UPDATE "saved_ref" SET uid = ?', {"saved_ref"}),
    ("SELECT * FROM equipment", set()),
])
def test_dml_targets(text, expected):
    assert dml_targets(text) == expected


def test_upsert_tail_is_not_a_table():
    assert dml_targets("INSERT INTO equipment (uid) VALUES (?) ON CONFLICT (uid) DO UPDATE SET name = excluded.name") == {"equipment", "set"}


@pytest.mark.parametrize("text, allowed", [
    ("UPDATE inventory SET uid = ? WHERE uid = ?", True),
    ("UPDATE saved_ref SET uid = :new WHERE uid = :old", True),
    ("UPDATE inventory SET uid = ?, note = ? WHERE uid = ?", False),
    ("UPDATE inventory SET note = ? WHERE uid = ?", False),
    ("DELETE FROM saved_ref WHERE uid = ?", True),
    ("DELETE FROM saved_ref", False),
    ("DELETE FROM inventory WHERE uid = ?", False),
])
def test_repoint_pattern(text, allowed):
    assert is_repoint(text, DML_RE.search(text).start()) is allowed


def test_write_scan(tmp_path):
    fake = tmp_path / "writer.py"
    fake.write_text(
        '"""UPDATE equipment SET cost = 1"""\n'
        "def f(conn, t):\n"
        '    """DELETE FROM stat"""\n'
        '    conn.execute("INSERT INTO equipment (uid) VALUES (?)")\n'
        '    conn.execute(f"DELETE FROM inventory WHERE uid = {t}")\n'
        '    conn.execute("UPDATE saved_ref SET uid = ? WHERE uid = ?")\n'
    )
    assert writes(fake, {"equipment", "stat"}) == ["equipment"]
    assert writes(fake, {"inventory", "saved_ref"}) == ["inventory", "saved_ref"]
    assert writes(fake, {"inventory", "saved_ref"}, allow_repoint=True) == ["inventory"]


def test_saved_ref_delete_allowed_only_in_sync_apply(tmp_path):
    user = {"inventory", "saved_ref"}
    delete_ref = 'def f(c):\n    c.execute("DELETE FROM saved_ref WHERE uid = ?")\n'
    delete_inv = 'def f(c):\n    c.execute("DELETE FROM inventory WHERE uid = ?")\n'
    cases = {
        ("sync", "apply.py"): (delete_ref, []),
        ("sync", "other.py"): (delete_ref, ["saved_ref"]),
        ("crawl", "run.py"): (delete_ref, ["saved_ref"]),
        ("sync", "apply.py", "inv"): (delete_inv, ["inventory"]),
    }
    for key, (source, expected) in cases.items():
        parts = key[:2]
        fake = tmp_path / "-".join(key) / "ul_house" / parts[0] / parts[1]
        fake.parent.mkdir(parents=True)
        fake.write_text(source)
        rel = fake.relative_to(tmp_path / "-".join(key) / "ul_house").parts
        assert writes(fake, user, allow_repoint=rel == ("sync", "apply.py")) == expected, key


# host left unnamed (guardrail)
def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := dotted(node.value)):
        return f"{base}.{node.attr}"
    return None


def wiki_host_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    package = package_of(path)
    config_names = set()
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "ul_house.config":
                    config_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = resolve(node, package)
            for alias in node.names:
                if module == "ul_house.config" and alias.name in ("BASE_URL", "*"):
                    hits.append(f"from ul_house.config import {alias.name}")
                elif module == "ul_house" and alias.name == "config":
                    config_names.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "BASE_URL" and dotted(node.value) in config_names:
            hits.append(f"{dotted(node.value)}.BASE_URL")
    return hits


def base_url_attributes(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [f"{dotted(node.value) or '<expr>'}.BASE_URL"
            for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "BASE_URL"]


def host_offences(path: Path) -> list[str]:
    return sorted(set(wiki_host_references(path)) | set(base_url_attributes(path)))


@pytest.mark.parametrize("subdir", ["app", "sync"])
def test_client_never_names_the_wiki_host(subdir):
    found = {label(p): hits for p in modules(subdir) if (hits := host_offences(p))}
    assert found == {}


def test_wiki_host_scan(tmp_path):
    fake = tmp_path / "ul_house" / "sync" / "icons.py"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "import ul_house.config\n"
        "import ul_house.config as c\n"
        "from ul_house import config as cfg\n"
        "from ..config import *\n"
        "a = ul_house.config.BASE_URL\n"
        "b = c.BASE_URL\n"
        "d = cfg.BASE_URL\n"
        "e = other.BASE_URL\n"
    )
    assert sorted(wiki_host_references(fake)) == sorted([
        "from ul_house.config import *", "ul_house.config.BASE_URL", "c.BASE_URL", "cfg.BASE_URL",
    ])


def test_wiki_host_scan_catches_attribute_access_without_a_config_import(tmp_path):
    fake = tmp_path / "ul_house" / "app" / "net.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("import ul_house\nurl = ul_house.config.BASE_URL\nother = thing().BASE_URL\n")
    assert wiki_host_references(fake) == []
    assert host_offences(fake) == ["<expr>.BASE_URL", "ul_house.config.BASE_URL"]


# ensure api independent of package
def test_api_is_independent():
    api = ROOT / "api"
    files = sorted(p for p in api.rglob("*.py") if "__pycache__" not in p.parts) if api.exists() else []
    assert offenders(files, ("ul_house",)) == {}
