"""Tests for run_testsuites command construction, failure handling, and cleanup."""

import pytest

from hls_utils import run_testsuites

GHC = "/nix/store/abc-ghc-9.6.7/bin/ghc"


def _make_logs(checkout):
    # run_for_version opens test-logs/<version>.log directly. main() makes the dir.
    (checkout / "test-logs").mkdir(parents=True, exist_ok=True)


def test_one_cabal_test_per_target(valid_checkout, make_recorder, install_run):
    _make_logs(valid_checkout)
    rec = install_run(run_testsuites, make_recorder())
    ok = run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, ["lib:a", "lib:b"]
    )
    assert ok is True
    assert len(rec.calls) == 3  # one probe + one cabal test per target
    assert len(rec.cabal_calls) == 2


def test_single_target_two_calls(valid_checkout, make_recorder, install_run):
    _make_logs(valid_checkout)
    rec = install_run(run_testsuites, make_recorder())
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, ["lib:a"])
    assert len(rec.cabal_calls) == 1


def test_cabal_test_command_shape(valid_checkout, make_recorder, install_run):
    _make_logs(valid_checkout)
    rec = install_run(run_testsuites, make_recorder())
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, ["lib:a"])
    cmd = rec.cabal_calls[0]
    assert cmd[:2] == ["cabal", "test"]
    assert cmd[cmd.index("-w") + 1] == GHC
    assert "--project-file=.hls-test.project" in cmd
    assert "--builddir=dist-newstyle/test-ghc96" in cmd
    assert "--test-show-details=streaming" in cmd
    assert cmd[cmd.index("--max-backjumps") + 1] == "10000"
    assert cmd[-1] == "lib:a"  # target is last
    cabal_kwargs = [k for c, k in rec.calls if c[-1] != "--numeric-version"][0]
    assert cabal_kwargs["cwd"] == valid_checkout


def test_returns_false_if_any_target_fails(valid_checkout, make_recorder, install_run):
    _make_logs(valid_checkout)
    install_run(run_testsuites, make_recorder(returncodes=[0, 1]))
    ok = run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, ["lib:a", "lib:b"]
    )
    assert ok is False


def test_returns_true_when_all_pass(valid_checkout, make_recorder, install_run):
    _make_logs(valid_checkout)
    install_run(run_testsuites, make_recorder(returncodes=[0, 0]))
    ok = run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, ["lib:a", "lib:b"]
    )
    assert ok is True


def test_log_header_records_numeric_version(
    valid_checkout, make_recorder, install_run, capsys
):
    _make_logs(valid_checkout)
    install_run(run_testsuites, make_recorder(probe_version="9.10.3"))
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, ["lib:a"])
    log = (valid_checkout / "test-logs" / "ghc96.log").read_text()
    assert log.startswith("GHC: 9.10.3")
    assert "OK" in capsys.readouterr().out


def test_failure_prints_tail(valid_checkout, make_recorder, install_run, capsys):
    _make_logs(valid_checkout)
    install_run(run_testsuites, make_recorder(returncodes=[1]))
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, ["lib:a"])
    assert "FAIL" in capsys.readouterr().out


# --- main() writes and always cleans up the temporary project file ---


def test_main_happy_path_cleans_project_file(
    valid_checkout, monkeypatch, make_recorder, install_run
):
    monkeypatch.setenv("HLS_GHCS", f'{{"ghc96": "{GHC}"}}')
    install_run(run_testsuites, make_recorder())
    assert run_testsuites.main(["ghc96"]) == 0
    assert not (valid_checkout / ".hls-test.project").exists()


def test_main_failure_still_cleans_project_file(
    valid_checkout, monkeypatch, make_recorder, install_run
):
    monkeypatch.setenv("HLS_GHCS", f'{{"ghc96": "{GHC}"}}')
    install_run(run_testsuites, make_recorder(returncodes=[1]))
    assert run_testsuites.main(["ghc96"]) == 1
    assert not (valid_checkout / ".hls-test.project").exists()


def test_main_cleans_project_file_on_exception(valid_checkout, monkeypatch):
    monkeypatch.setenv("HLS_GHCS", f'{{"ghc96": "{GHC}"}}')

    def boom(*args, **kwargs):
        raise RuntimeError("subprocess blew up")

    monkeypatch.setattr(run_testsuites.subprocess, "run", boom)
    with pytest.raises(RuntimeError):
        run_testsuites.main(["ghc96"])
    assert not (valid_checkout / ".hls-test.project").exists()
