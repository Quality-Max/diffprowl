from examples.input_demo.app.service import direct_value


def test_second(cached):
    assert direct_value() == 2
    assert cached == 1
