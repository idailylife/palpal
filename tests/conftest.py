import pytest

from tests.support import make_debug_state


@pytest.fixture
def debug_state(tmp_path):
    return make_debug_state(tmp_path)
