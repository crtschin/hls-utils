"""Tests for the shared helpers in hls_utils._common."""

import json

import pytest

from hls_utils import _common

# --- load_ghc_map ---


def test_no_env_returns_empty():
    assert _common.load_ghc_map() == {}


def test_empty_inline_returns_empty(monkeypatch):
    monkeypatch.setenv("HLS_GHCS", "")
    assert _common.load_ghc_map() == {}


def test_inline_json(monkeypatch):
    monkeypatch.setenv("HLS_GHCS", '{"ghc96": "/p/ghc"}')
    assert _common.load_ghc_map() == {"ghc96": "/p/ghc"}


def test_file_wins_over_inline(tmp_path, monkeypatch):
    f = tmp_path / "ghcs.json"
    f.write_text('{"ghc98": "/from/file"}')
    monkeypatch.setenv("HLS_GHCS_FILE", str(f))
    monkeypatch.setenv("HLS_GHCS", '{"ghc96": "/from/inline"}')
    assert _common.load_ghc_map() == {"ghc98": "/from/file"}


@pytest.mark.parametrize("payload", ["[]", '"x"', "5", "null"])
def test_non_dict_returns_empty(monkeypatch, payload):
    monkeypatch.setenv("HLS_GHCS", payload)
    assert _common.load_ghc_map() == {}


def test_coerces_values_to_str(monkeypatch):
    monkeypatch.setenv("HLS_GHCS", '{"96": 96}')
    assert _common.load_ghc_map() == {"96": "96"}


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HLS_GHCS_FILE", str(tmp_path / "does-not-exist.json"))
    assert _common.load_ghc_map() == {}


def test_malformed_file_raises(tmp_path, monkeypatch):
    f = tmp_path / "bad.json"
    f.write_text("not json {{{")
    monkeypatch.setenv("HLS_GHCS_FILE", str(f))
    with pytest.raises(json.JSONDecodeError):
        _common.load_ghc_map()


# --- find_hls_checkout ---


def test_finds_cabal_project_in_start(tmp_path):
    (tmp_path / "cabal.project").write_text("")
    assert _common.find_hls_checkout(tmp_path) == tmp_path


def test_finds_cabal_project_in_ancestor(tmp_path):
    (tmp_path / "cabal.project").write_text("")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert _common.find_hls_checkout(nested) == tmp_path


def test_returns_none_when_absent(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert _common.find_hls_checkout(nested) is None


def test_ignores_directory_named_cabal_project(tmp_path):
    (tmp_path / "cabal.project").mkdir()
    assert _common.find_hls_checkout(tmp_path) is None
