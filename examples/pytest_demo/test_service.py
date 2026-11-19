from examples.pytest_demo.service import remaining, total


def test_total():
    assert total(3, 5) == 15


def test_remaining():
    assert remaining(10, 4) == 6
