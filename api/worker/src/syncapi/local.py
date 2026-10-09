"""local dev: uvicorn --factory syncapi.local:create_local_app"""

from __future__ import annotations

import os
from pathlib import Path

from syncapi.app import COMMIT_RE, create_app
from syncapi.storage import LocalPubStore

ROOT_VAR = "UL_HOUSE_STORE_ROOT"
COMMIT_ENV = "UL_HOUSE_API_COMMIT"


def create_local_app():
    root_text = os.environ.get(ROOT_VAR)
    if not root_text:
        raise SystemExit(f"{ROOT_VAR} not set: point it at a store root that contains pub/")
    root = Path(root_text)
    if not (root / "pub").is_dir():
        raise SystemExit(f"{ROOT_VAR}={root_text!r} has no pub/ directory")
    commit = os.environ.get(COMMIT_ENV, "dev")
    if not COMMIT_RE.fullmatch(commit):
        raise SystemExit(f"{COMMIT_ENV} must be 40 lowercase hex characters or 'dev'")
    return create_app(LocalPubStore(root), api_commit=commit)
