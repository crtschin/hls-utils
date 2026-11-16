import subprocess

import pytest

from hls_utils import run_testsuites

GHC = "/nix/store/abc-ghc-9.6.7/bin/ghc"


def _make_logs(checkout):
    logs = checkout / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def test_one_cabal_test_per_target(valid_checkout, make_recorder, install_run):
    logs = _make_logs(valid_checkout)
    rec = install_run(make_recorder())
    ok = run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, logs, ["lib:a", "lib:b"]
    )
    assert ok is True
    assert len(rec.calls) == 3  # one probe + one cabal test per target
    assert len(rec.cabal_calls) == 2


def test_single_target_two_calls(valid_checkout, make_recorder, install_run):
    logs = _make_logs(valid_checkout)
    rec = install_run(make_recorder())
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, logs, ["lib:a"])
    assert len(rec.cabal_calls) == 1


def test_cabal_test_command_shape(valid_checkout, make_recorder, install_run):
    logs = _make_logs(valid_checkout)
    rec = install_run(make_recorder())
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, logs, ["lib:a"])
    cmd = rec.cabal_calls[0]
    assert cmd[:2] == ["cabal", "test"]
    assert cmd[cmd.index("-w") + 1] == GHC
    assert "--project-file=.hls-test.project" in cmd
    assert "--builddir=dist-newstyle/test-ghc96" in cmd
    assert "--test-show-details=streaming" in cmd
    assert cmd[cmd.index("--max-backjumps") + 1] == "10000"
    assert cmd[-1] == "lib:a"
    cabal_kwargs = [k for c, k in rec.calls if c[-1] != "--numeric-version"][0]
    assert cabal_kwargs["cwd"] == valid_checkout


def test_returns_false_if_any_target_fails(valid_checkout, make_recorder, install_run):
    logs = _make_logs(valid_checkout)
    install_run(make_recorder(returncodes=[0, 1]))
    ok = run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, logs, ["lib:a", "lib:b"]
    )
    assert ok is False


def test_returns_true_when_all_pass(valid_checkout, make_recorder, install_run):
    logs = _make_logs(valid_checkout)
    install_run(make_recorder(returncodes=[0, 0]))
    ok = run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, logs, ["lib:a", "lib:b"]
    )
    assert ok is True


def test_log_header_records_numeric_version(
    valid_checkout, make_recorder, install_run, capsys
):
    logs = _make_logs(valid_checkout)
    install_run(make_recorder(probe_version="9.10.3"))
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, logs, ["lib:a"])
    log = (logs / "ghc96.log").read_text()
    assert log.startswith("GHC: 9.10.3")
    assert "OK" in capsys.readouterr().out


def test_failure_prints_tail(valid_checkout, make_recorder, install_run, capsys):
    logs = _make_logs(valid_checkout)
    install_run(make_recorder(returncodes=[1]))
    run_testsuites.run_for_version("ghc96", GHC, valid_checkout, logs, ["lib:a"])
    assert "FAIL" in capsys.readouterr().out


def test_cabal_test_env_keeps_build_paths(
    valid_checkout, make_recorder, install_run, monkeypatch
):
    monkeypatch.setenv("LIBRARY_PATH", "/nix/store/abc-zlib/lib")
    monkeypatch.setenv("C_INCLUDE_PATH", "/nix/store/abc-zlib-dev/include")
    rec = install_run(make_recorder())
    run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, _make_logs(valid_checkout), ["lib:a"]
    )
    env = [k for c, k in rec.calls if c[-1] != "--numeric-version"][0]["env"]
    assert env["LIBRARY_PATH"] == "/nix/store/abc-zlib/lib"
    assert env["C_INCLUDE_PATH"] == "/nix/store/abc-zlib-dev/include"


def test_cabal_test_env_puts_ghc_first_on_path(
    valid_checkout, make_recorder, install_run, monkeypatch
):
    monkeypatch.setenv("PATH", "/usr/bin")
    rec = install_run(make_recorder())
    run_testsuites.run_for_version(
        "ghc96", GHC, valid_checkout, _make_logs(valid_checkout), ["lib:a"]
    )
    env = [k for c, k in rec.calls if c[-1] != "--numeric-version"][0]["env"]
    assert env["PATH"] == "/nix/store/abc-ghc-9.6.7/bin:/usr/bin"


def test_main_sweeps_every_version(
    set_ghcs, valid_checkout, monkeypatch, make_recorder, install_run
):
    other = "/nix/store/def-ghc-9.8.4/bin/ghc"
    set_ghcs({"ghc96": GHC, "ghc98": other})
    rec = install_run(make_recorder(returncodes=[1]))
    assert run_testsuites.main(["ghc96", "ghc98"]) == 1
    assert [cmd[cmd.index("-w") + 1] for cmd in rec.cabal_calls] == [GHC, other]


def test_main_happy_path_cleans_project_file(
    set_ghcs, valid_checkout, monkeypatch, make_recorder, install_run
):
    set_ghcs({"ghc96": GHC})
    install_run(make_recorder())
    assert run_testsuites.main(["ghc96"]) == 0
    assert not (valid_checkout / ".hls-test.project").exists()


def test_main_failure_still_cleans_project_file(
    set_ghcs, valid_checkout, monkeypatch, make_recorder, install_run
):
    set_ghcs({"ghc96": GHC})
    install_run(make_recorder(returncodes=[1]))
    assert run_testsuites.main(["ghc96"]) == 1
    assert not (valid_checkout / ".hls-test.project").exists()


def test_main_cleans_project_file_on_exception(set_ghcs, valid_checkout, monkeypatch):
    set_ghcs({"ghc96": GHC})

    def boom(*args, **kwargs):
        raise RuntimeError("subprocess blew up")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError):
        run_testsuites.main(["ghc96"])
    assert not (valid_checkout / ".hls-test.project").exists()
