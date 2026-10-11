"""constants and the contract loader shared by api/tests modules"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = HERE.parent
CONTRACT = API / "contract"
SCHEMAS = CONTRACT / "schemas"
WORKER_SRC = API / "worker" / "src"

for entry in (WORKER_SRC, HERE / "fixtures", HERE):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

SCHEMA_NAMES = ("current", "manifest", "update_package", "icon_pack", "requires_snapshot")

class Contract:
    """the five JSON Schemas and openapi.yaml, with validators"""

    def __init__(self) -> None:
        import yaml

        self.schemas = {name: json.loads((SCHEMAS / f"{name}.schema.json").read_text()) for name in SCHEMA_NAMES}
        self.openapi = yaml.safe_load((CONTRACT / "openapi.yaml").read_text())

    @property
    def error_schema(self) -> dict:
        return self.openapi["components"]["schemas"]["Error"]

    def validator(self, name: str):
        from jsonschema import Draft202012Validator

        return Draft202012Validator(self.schemas[name])

    def errors(self, name: str, instance) -> list[str]:
        return [error.message for error in self.validator(name).iter_errors(instance)]

    def validate(self, name: str, instance) -> None:
        errors = self.errors(name, instance)
        assert not errors, f"{name}: {errors[:3]}"

    def error_errors(self, instance) -> list[str]:
        from jsonschema import Draft202012Validator

        return [error.message for error in Draft202012Validator(self.error_schema).iter_errors(instance)]

    def validate_error(self, instance) -> None:
        errors = self.error_errors(instance)
        assert not errors, f"Error: {errors[:3]}"

    def decoded_schema(self, openapi_path: str, media_type: str) -> dict:
        """the schema that validates the DECODED body of an openapi path's 200 response of this media type"""
        content = self.openapi["paths"][openapi_path]["get"]["responses"]["200"]["content"][media_type]
        ref = (content.get("x-decoded-schema") or content["schema"])["$ref"]
        return self.schemas[Path(ref).name.removesuffix(".schema.json")]


