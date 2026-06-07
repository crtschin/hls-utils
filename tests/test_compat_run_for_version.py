"""Tests for check_ghc_compat command construction.

Contrast with run_testsuites: compat builds all targets in a SINGLE cabal call,
not one invocation per target.
"""

from hls_utils import check_ghc_compat

GHC = "/nix/store/abc-ghc-9.6.7/bin/ghc"


def test_default_single_build_with_pedantic_and_disable_tests(
    valid_checkout, monkeypatch, make_recorder, install_run
):
    monkeypatch.setenv("HLS_GHCS", f'{{"ghc96": "{GHC}"}}')
    rec = install_run(check_ghc_compat, make_recorder())
    assert check_ghc_compat.main(["ghc96"]) == 0
    assert len(rec.cabal_calls) == 1  # a single cabal build, no per-target loop
    cmd = rec.cabal_calls[0]
    assert cmd[:2] == ["cabal", "build"]
    assert cmd[cmd.index("--constraint") + 1] == "haskell-language-server +pedantic"
    assert "--disable-tests" in cmd
    assert "--builddir=dist-newstyle/compat-ghc96" in cmd
    assert "--project-file=.hls-compat.project" in cmd
    assert cmd[-1] == "all"


def test_explicit_targets_drop_disable_tests(
    valid_checkout, monkeypatch, make_recorder, install_run
):
    monkeypatch.setenv("HLS_GHCS", f'{{"ghc96": "{GHC}"}}')
    rec = install_run(check_ghc_compat, make_recorder())
    assert check_ghc_compat.main(["ghc96", "-t", "x", "y"]) == 0
    assert len(rec.cabal_calls) == 1
    cmd = rec.cabal_calls[0]
    assert "--disable-tests" not in cmd
    assert cmd[-2:] == ["x", "y"]


def test_multi_version_runs_all_and_accumulates_failure(
    valid_checkout, monkeypatch, make_recorder, install_run
):
    monkeypatch.setenv("HLS_GHCS", f'{{"ghc96": "{GHC}", "ghc98": "{GHC}"}}')
    rec = install_run(check_ghc_compat, make_recorder(returncodes=[1, 0]))
    assert check_ghc_compat.main(["ghc96", "ghc98"]) == 1
    assert len(rec.cabal_calls) == 2  # both versions build despite the first failing


def test_mixed_known_unknown(
    valid_checkout, monkeypatch, make_recorder, install_run, capsys
):
    monkeypatch.setenv("HLS_GHCS", f'{{"ghc96": "{GHC}"}}')
    rec = install_run(check_ghc_compat, make_recorder())
    assert check_ghc_compat.main(["ghc96", "ghcXX"]) == 1
    assert len(rec.cabal_calls) == 1  # only the known version builds
    assert "unknown version" in capsys.readouterr().out
    assert not (valid_checkout / ".hls-compat.project").exists()
