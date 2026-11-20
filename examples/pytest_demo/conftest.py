"""Explicit local fixture inputs for the qualification example."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def quantity():
    return 3


@pytest.fixture
def unit_price():
    return json.loads(Path(__file__).with_name("settings.json").read_text())["unit_price"]
