"""Tests for main() exit codes and early-return paths (before any cabal call)."""

from hls_utils import check_ghc_compat, run_testsuites


def test_rt_no_checkout_returns_2(
    tmp_path, monkeypatch, capsys, make_recorder, install_run
):
    monkeypatch.chdir(tmp_path)  # no cabal.project here or above
    rec = install_run(run_testsuites, make_recorder())
    assert run_testsuites.main(["ghc96"]) == 2
    assert "no cabal.project found" in capsys.readouterr().err
    assert rec.calls == []
    assert not (tmp_path / ".hls-test.project").exists()


def test_compat_no_checkout_returns_2(
    tmp_path, monkeypatch, capsys, make_recorder, install_run
):
    monkeypatch.chdir(tmp_path)
    rec = install_run(check_ghc_compat, make_recorder())
    assert check_ghc_compat.main(["ghc96"]) == 2
    assert "no cabal.project found" in capsys.readouterr().err
    assert rec.calls == []


def test_rt_unknown_version_returns_1_without_touching_fs(
    valid_checkout, monkeypatch, capsys, make_recorder, install_run
):
    monkeypatch.setenv("HLS_GHCS", '{"ghc98": "/p/ghc"}')
    rec = install_run(run_testsuites, make_recorder())
    assert run_testsuites.main(["ghc96"]) == 1
    assert "unknown version (have: ghc98)" in capsys.readouterr().out
    assert rec.calls == []
    # The unknown-version check precedes the project-file write.
    assert not (valid_checkout / ".hls-test.project").exists()


def test_compat_unknown_version_returns_1_and_cleans_up(
    valid_checkout, monkeypatch, capsys, make_recorder, install_run
):
    monkeypatch.setenv("HLS_GHCS", '{"ghc98": "/p/ghc"}')
    rec = install_run(check_ghc_compat, make_recorder())
    assert check_ghc_compat.main(["ghc96"]) == 1
    assert "unknown version (have: ghc98)" in capsys.readouterr().out
    assert rec.calls == []
    # compat writes its project file before the loop, then cleans it in finally.
    assert not (valid_checkout / ".hls-compat.project").exists()


def test_rt_unknown_version_empty_map_diagnostic(
    valid_checkout, make_recorder, install_run, capsys
):
    install_run(run_testsuites, make_recorder())
    assert run_testsuites.main(["ghc96"]) == 1
    assert "have: (none)" in capsys.readouterr().out
