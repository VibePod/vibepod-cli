"""Everything under tests/integration requires a live container runtime."""

from pathlib import Path

import pytest

_INTEGRATION_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        # This hook receives the whole session's items, not just this
        # directory's — scope the marker to tests that live under it.
        if not item.path.is_relative_to(_INTEGRATION_DIR):
            continue
        # Check real markers, not item.keywords: keywords include ancestor
        # node names, and this directory is itself named "integration".
        if item.get_closest_marker("integration") is None:
            item.add_marker(pytest.mark.integration)
