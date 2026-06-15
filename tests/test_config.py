import logging

import pytest

from ebayspy.config import _bool_env, _float_env, _int_env, _source_env


def test_bool_env_recognizes_true_and_false(monkeypatch) -> None:
    for raw in ("1", "true", "YES", " on "):
        monkeypatch.setenv("FLAG", raw)
        assert _bool_env("FLAG", False) is True
    for raw in ("0", "false", "NO", "off"):
        monkeypatch.setenv("FLAG", raw)
        assert _bool_env("FLAG", True) is False
    monkeypatch.delenv("FLAG", raising=False)
    assert _bool_env("FLAG", True) is True  # unset -> default


def test_bool_env_warns_and_keeps_default_on_unrecognized(monkeypatch, caplog) -> None:
    # The old behaviour silently turned a default-on feature off on a typo;
    # now it warns and keeps the default so the feature isn't disabled.
    monkeypatch.setenv("FLAG", "ture")
    with caplog.at_level(logging.WARNING):
        assert _bool_env("FLAG", True) is True
        assert _bool_env("FLAG", False) is False
    assert "FLAG" in caplog.text and "boolean" in caplog.text


def test_int_and_float_env_raise_named_error(monkeypatch) -> None:
    monkeypatch.setenv("N", "15m")
    with pytest.raises(ValueError, match="N must be a whole number"):
        _int_env("N", 1)
    monkeypatch.setenv("R", "abc")
    with pytest.raises(ValueError, match="R must be a number"):
        _float_env("R", 1.0)


def test_int_float_env_use_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("N", raising=False)
    assert _int_env("N", 7) == 7
    assert _float_env("R", 1.5) == 1.5


def test_source_env_falls_back_on_invalid(monkeypatch, caplog) -> None:
    monkeypatch.setenv("SRC", "insight")  # typo, missing trailing 's'
    with caplog.at_level(logging.WARNING):
        assert _source_env("SRC", "listings", {"listings", "insights"}) == "listings"
    assert "SRC" in caplog.text
    monkeypatch.setenv("SRC", "INSIGHTS")
    assert _source_env("SRC", "listings", {"listings", "insights"}) == "insights"
