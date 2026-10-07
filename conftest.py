"""pytest configs (for tests/ and api/tests/)"""

def pytest_addoption(parser):
    parser.addoption(
        "--no-fetch",
        action="store_true",
        default=False,
        help="do not download missing fixture pages (skip tests that need them)",
    )
