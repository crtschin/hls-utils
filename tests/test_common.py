import json

import pytest

from hls_utils import _common

# Bound here because the temp_sweep_logs fixture stubs the module attribute out.
real_sweep_log_dir = _common.sweep_log_dir


def test_missing_file_returns_empty(tmp_path):
    assert _common.load_ghc_map(str(tmp_path / "does-not-exist.json")) == {}


def test_unsubstituted_placeholder_returns_empty():
    assert _common.load_ghc_map() == {}


def test_keeps_file_order(tmp_path):
    f = tmp_path / "ghcs.json"
    f.write_text(json.dumps([["ghc96", "/a/ghc"], ["ghc910", "/b/ghc"]]))
    assert list(_common.load_ghc_map(str(f)).items()) == [
        ("ghc96", "/a/ghc"),
        ("ghc910", "/b/ghc"),
    ]


def test_malformed_file_raises(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json {{{")
    with pytest.raises(json.JSONDecodeError):
        _common.load_ghc_map(str(f))


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


def test_sweep_log_dir_under_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(_common.tempfile, "gettempdir", lambda: str(tmp_path))
    d = real_sweep_log_dir("compat")
    assert d == tmp_path / "hls-utils-logs" / "compat"
    assert d.is_dir()
    assert real_sweep_log_dir("compat") == d
    assert real_sweep_log_dir("test") == tmp_path / "hls-utils-logs" / "test"
