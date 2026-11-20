import pytest
from examples.input_demo.app.service import cached_value


@pytest.fixture(scope="session")
def cached():
    return cached_value()


@pytest.fixture
def configured():
    return 3
