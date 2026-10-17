import os
import pytest


@pytest.fixture(autouse=True)
def isolate_deye_cache(tmp_path, monkeypatch):
    """
    Isolates Deye inverter cache file to a clean per-test temporary file.
    Prevents cache state crosstalk between test cases.
    """
    test_cache = str(tmp_path / ".deye_inverter_cache.json")
    monkeypatch.setenv("DEYE_CACHE_FILE", test_cache)
    yield test_cache
