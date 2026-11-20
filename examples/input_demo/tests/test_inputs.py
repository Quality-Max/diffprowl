import json
from pathlib import Path
from examples.input_demo.app import VERSION
from examples.input_demo.app.service import direct_value
from examples.input_demo.tests.helpers import expected


def test_helper():
    assert direct_value() == expected()


def test_import():
    assert VERSION == 1


def test_resource():
    assert json.loads(Path("examples/input_demo/settings.json").read_text())["value"] == 4


def test_configured(configured):
    assert configured == 3
