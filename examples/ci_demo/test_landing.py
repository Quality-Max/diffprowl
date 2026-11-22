from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def session_context():
    yield


@pytest.fixture
def landing_html():
    return Path(__file__).with_name("landing.html").read_text()


def test_heading(landing_html):
    assert "<h1>QA Wiki</h1>" in landing_html


def test_resources_link(landing_html):
    assert 'href="/resources"' in landing_html
