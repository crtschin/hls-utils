"""Tests for main(): argparse contract and exit codes, with git faked."""

import json

import pytest

from hls_utils import ghc_backports as gb


def test_missing_target_exits_2():
    with pytest.raises(SystemExit) as exc:
        gb.main([])
    assert exc.value.code == 2


def test_extra_positional_exits_2():
    with pytest.raises(SystemExit) as exc:
        gb.main(["#1", "extra"])
    assert exc.value.code == 2


def test_no_repo_returns_2_without_touching_git(make_git_recorder, install_run, capsys):
    rec = install_run(gb, make_git_recorder(lambda argv: (0, "")))
    assert gb.main(["#1"]) == 2
    assert "GHC_REPO" in capsys.readouterr().err
    assert rec.calls == []  # resolve_repo fails before any git call


def test_invalid_repo_returns_2(make_git_recorder, install_run, capsys):
    rec = install_run(gb, make_git_recorder(lambda argv: (1, "not a git repository")))
    assert gb.main(["--repo", "/x", "#1"]) == 2
    assert "error:" in capsys.readouterr().err
    assert rec.calls == [["rev-parse", "--git-dir"]]


def test_nothing_found_returns_1(make_git_recorder, install_run, capsys):
    def responder(argv):
        if argv[:2] == ["rev-parse", "--git-dir"]:
            return (0, ".git")
        return (0, "")  # the issue grep finds nothing

    install_run(gb, make_git_recorder(responder))
    assert gb.main(["--repo", "/repo", "#999"]) == 1
    assert "no commits found" in capsys.readouterr().err


def _success_responder(argv):
    if argv[:2] == ["rev-parse", "--git-dir"]:
        return (0, ".git")
    if argv[:2] == ["rev-parse", "--verify"]:
        return (0, "a" * 40)
    if argv[:2] == ["show", "-s"]:
        return (0, "Fix foo")
    if argv[0] == "tag":
        return (0, "ghc-9.12.1-release")
    if argv[0] == "branch":
        return (0, "* master\n  remotes/origin/ghc-9.12")
    if argv[0] == "log":
        # Subject grep matches the seed itself; cherry-pick greps find nothing.
        return (0, "a" * 40) if "--grep=Fix foo" in argv else (0, "")
    return (0, "")


def test_success_human_output(make_git_recorder, install_run, capsys):
    install_run(gb, make_git_recorder(_success_responder))
    assert gb.main(["--repo", "/repo", "aaaaaaa"]) == 0
    out = capsys.readouterr().out
    assert "9.12: 9.12.1+" in out
    assert "present on master" in out


def test_success_json_output(make_git_recorder, install_run, capsys):
    install_run(gb, make_git_recorder(_success_responder))
    assert gb.main(["--repo", "/repo", "--json", "aaaaaaa"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["released"] == {"9.12": "9.12.1"}
    assert report["coverage"]["on_master"] is True
