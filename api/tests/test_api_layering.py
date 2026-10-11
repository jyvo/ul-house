"""boundary tests for the API worker source tree
    - api/ imports nothing from ul_house
    - worker import discipline
    - wrangler / entry-point wiring
"""
from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]
WORKER = API / "worker"
SRC = WORKER / "src"
SYNCAPI = SRC / "syncapi"

STDLIB = set(sys.stdlib_module_names)
WORKER_ALLOWED = STDLIB | {"fastapi", "starlette", "syncapi", "workers", "asgi", "js", "pyodide"}


def py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imports_of(path: Path | str, *, source: str | None = None) -> set[str]:
    tree = ast.parse(source if source is not None else Path(path).read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
                continue
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def dynamic_imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant):
                out.add(str(node.args[0].value))
    return out


def top(name: str) -> str:
    return name.split(".")[0]


def label(path: Path) -> str:
    return str(path.relative_to(API))


def is_ul_house(name: str) -> bool:
    return name == "ul_house" or name.startswith("ul_house.")


# boundary
def test_the_scan_sees_the_api_tree():
    files = py_files(API)
    assert len(files) >= 15, [label(p) for p in files]
    assert any(imports_of(p) for p in files)
    assert "fastapi" in imports_of(SYNCAPI / "app.py")
    assert "syncapi.routing" in imports_of(SYNCAPI / "plain.py")


def test_the_scanner_detects_a_violation():
    assert any(is_ul_house(n) for n in imports_of("x.py", source="import ul_house.sync"))
    assert any(is_ul_house(n) for n in imports_of("x.py", source="from ul_house import ids"))
    assert any(is_ul_house(n) for n in imports_of("x.py", source="from ul_house.sync.verify import canonical_bytes"))
    assert not any(is_ul_house(n) for n in imports_of("x.py", source="import ul_house_api_fixture\nfrom ul_housekeeping import x"))


def test_api_imports_nothing_from_ul_house():
    offenders = []
    for path in py_files(API):
        for name in imports_of(path) | dynamic_imports_of(path):
            if is_ul_house(name):
                offenders.append(f"{label(path)} imports {name}")
    assert not offenders, offenders


def test_no_init_files_in_the_test_tree():
    assert list((API / "tests").rglob("__init__.py")) == []


# worker import discipline
@pytest.mark.parametrize("path", py_files(SRC), ids=lambda p: label(p))
def test_worker_source_imports_only_the_allowed_modules(path):
    for name in imports_of(path):
        if name.startswith("."):
            continue
        assert top(name) in WORKER_ALLOWED, f"{label(path)} imports {name}"


def local_closure(start: Path) -> set[Path]:
    seen: set[Path] = set()
    todo = [start]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in imports_of(path):
            if top(name) == "syncapi":
                parts = name.split(".")[1:]
                candidate = SYNCAPI.joinpath(*parts).with_suffix(".py") if parts else SYNCAPI / "__init__.py"
                if candidate.is_file():
                    todo.append(candidate)
    return seen


@pytest.mark.parametrize("module", ["plain", "routing"])
def test_pure_modules_pull_in_no_web_framework(module):
    for path in local_closure(SYNCAPI / f"{module}.py"):
        for name in imports_of(path):
            assert top(name) not in {"fastapi", "starlette", "pydantic", "uvicorn", "httpx"}, f"{module} -> {label(path)} imports {name}"


def test_routing_is_stdlib_only():
    for name in imports_of(SYNCAPI / "routing.py"):
        assert top(name) in STDLIB, name


def test_storage_r2_has_no_top_level_platform_imports():
    tree = ast.parse((SYNCAPI / "storage_r2.py").read_text())
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
            for name in names:
                assert top(name) not in {"js", "pyodide", "workers", "asgi"}, name


# no per request validation
def test_no_response_model_keyword_in_worker_source():
    for path in py_files(SRC):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword):
                assert node.arg != "response_model", f"{label(path)}:{node.value.lineno}"


def test_no_pydantic_import_in_worker_source():
    for path in py_files(SRC):
        for name in imports_of(path):
            assert top(name) != "pydantic", f"{label(path)} imports {name}"


# wrangler.jsonc
def wrangler() -> dict:
    text = (WORKER / "wrangler.jsonc").read_text()
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    return json.loads("\n".join(lines))


def test_wrangler_config():
    config = wrangler()
    assert "python_workers" in config["compatibility_flags"]
    assert (WORKER / config["main"]).is_file()
    from syncapi.app import BUCKET_BINDING, COMMIT_VAR
    from syncapi.plain import BUCKET_BINDING as PLAIN_BINDING

    assert config["r2_buckets"][0]["binding"] == BUCKET_BINDING == PLAIN_BINDING
    assert COMMIT_VAR in config["vars"]
    assert config["observability"]["enabled"] is False


def test_wrangler_placeholders_of_p21():
    config = wrangler()
    assert config["name"] == "ul-house-api"
    assert config["r2_buckets"][0]["bucket_name"] == "ul-house"
    assert config["compatibility_date"] == "2026-09-01"
    assert config["workers_dev"] is True
    assert config["vars"]["API_COMMIT"] == "dev"


def test_wrangler_exposes_nothing_else():
    config = wrangler()
    text = (WORKER / "wrangler.jsonc").read_text()
    assert "r2.dev" not in text
    for forbidden in ("routes", "route", "kv_namespaces", "d1_databases", "services", "triggers", "durable_objects"):
        assert forbidden not in config, forbidden
    assert len(config["r2_buckets"]) == 1


def test_worker_pyproject():
    data = tomllib.loads((WORKER / "pyproject.toml").read_text())
    assert data["project"]["name"] == "ul-house-api-worker"
    assert any(dep.startswith("fastapi") for dep in data["project"]["dependencies"])
    assert not any("ul-house" == dep.split("[")[0].strip() or dep.startswith("ul_house") for dep in data["project"]["dependencies"])


# entry points
def test_entry_py_imports():
    names = {top(n) if not n.startswith("syncapi") else n for n in imports_of(SRC / "entry.py")}
    assert {n for n in names if not n.startswith("syncapi.")} <= {"workers", "asgi", "syncapi"}
    assert {n for n in imports_of(SRC / "entry.py") if top(n) == "syncapi"} <= {"syncapi", "syncapi.app", "syncapi.app.create_app"}
    assert "syncapi.app" in imports_of(SRC / "entry.py")


def test_entry_plain_py_imports():
    imported = imports_of(SRC / "entry_plain.py")
    assert {top(n) for n in imported} <= {"workers", "syncapi"}
    assert {n for n in imported if top(n) == "syncapi"} <= {"syncapi", "syncapi.plain", "syncapi.plain.handle"}
    assert "syncapi.plain" in imported


@pytest.mark.parametrize("entry", ["entry.py", "entry_plain.py"])
def test_entries_parse_and_define_default(entry):
    tree = ast.parse((SRC / entry).read_text())
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "Default" in classes


def test_the_worker_entries_are_not_importable_on_cpython():
    for entry in ("entry", "entry_plain"):
        sys.modules.pop(entry, None)
        with pytest.raises(ImportError):
            __import__(entry)
