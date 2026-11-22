from unittest.mock import Mock

import pytest
from failure_source import UnsupportedModel
from worker import index_registered


def test_permanent_error_wins_after_transient_error():
    retry = Mock()
    with pytest.raises(UnsupportedModel):
        index_registered(["first", "second"], retry)
    retry.assert_not_called()


def test_transient_error_is_retried():
    retry = Mock()
    index_registered(["first", "healthy"], retry)
    retry.assert_called_once()
    assert isinstance(retry.call_args.args[0], ConnectionError)
