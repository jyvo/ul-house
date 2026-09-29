"""populate local cache (test/data); gitignored, every checkout fetches its own copy"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ul_house.config import USER_AGENT, detail_url  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"

# each page is kept for the parser path
UIDS: dict[str, str] = {
    "1015655": "weapon with proc, weapon ability and reforge",
    "1015157": "weapon with awakening + materials, no ability, empty reforge",
    "1890424": "defensive gear with proc (scaling in [effects])",
    "1796604": "monster with one skill, a passive and hidden potential",
    "1500502": "monster with two skills and potential restrictions",
    "4425111": "monster awakening with no materials, fixed def/mdef stats",
    "4435013": "monster enlightening with materials, five potential levels",
    "4434015": "ssr monster with reforge materials and no hidden potential",
}

DELAY_SECONDS = 0.5
TIMEOUT_SECONDS = 30


def cached_path(uid: str) -> Path:
    return DATA_DIR / f"{uid}.html"


def missing() -> list[str]:
    return [uid for uid in UIDS if not cached_path(uid).exists()]


def fetch(uid: str) -> None:
    response = requests.get(
        detail_url(uid), headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS
    )
    response.raise_for_status()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cached_path(uid).write_text(response.text)


def ensure_cached(force: bool = False, quiet: bool = False) -> list[str]:
    """
        - pytest calls at beginning, fetch whatever is missing (pass --no-fetch to turn off)
        - returns the uids still unavailable
    """
    wanted = list(UIDS) if force else missing()
    if not wanted:
        return []

    if not quiet:
        print(f"caching {len(wanted)} fixture page(s) into {DATA_DIR}", file=sys.stderr)

    failed = []
    for index, uid in enumerate(wanted):
        if index:
            time.sleep(DELAY_SECONDS)
        try:
            fetch(uid)
            if not quiet:
                print(f"  {uid}  {UIDS[uid]}", file=sys.stderr)
        except Exception as error:  # network, http, disk = "no fixture"
            failed.append(uid)
            if not quiet:
                print(f"  {uid}  FAILED: {type(error).__name__}: {error}", file=sys.stderr)
    return failed


def main() -> int:
    """
        - python tests/fetch_fixtures.py            # fetch missing
        - python tests/fetch_fixtures.py --force    # re-fetch all
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-fetch pages already cached")
    args = parser.parse_args()

    failed = ensure_cached(force=args.force)
    still_missing = missing()
    if still_missing:
        print(f"\n{len(still_missing)} page(s) unavailable: {', '.join(still_missing)}", file=sys.stderr)
        return 1
    print(f"\n{len(UIDS)} page(s) cached in {DATA_DIR}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
