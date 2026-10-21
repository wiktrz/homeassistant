import os
import sys

import pytest

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config"))
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

import solarman_v5_client


@pytest.fixture(autouse=True)
def _reset_solarman_deadline():
    # The local Modbus time budget is once-per-process; don't let it leak between tests.
    solarman_v5_client._deadline = None
    yield
    solarman_v5_client._deadline = None
