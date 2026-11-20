from examples.pytest_demo.service import remaining, total


def test_total(quantity, unit_price):
    assert total(quantity, unit_price) == 15


def test_remaining():
    assert remaining(10, 4) == 6
