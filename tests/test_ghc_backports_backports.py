"""Tests for commit resolution + backport expansion, with git faked."""

from pathlib import Path

from hls_utils import ghc_backports as gb

REPO = Path("/repo")


def test_seed_commits_sha_resolves_full(make_git_recorder, install_run):
    def responder(argv):
        if argv[:2] == ["rev-parse", "--verify"]:
            return (0, "f" * 40)
        return (0, "")

    rec = install_run(gb, make_git_recorder(responder))
    seeds, note = gb.seed_commits(REPO, "sha", "abc1234")
    assert seeds == ["f" * 40]
    assert note is None
    assert rec.calls[0] == ["rev-parse", "--verify", "abc1234^{commit}"]


def test_seed_commits_issue_uses_anchored_pattern(make_git_recorder, install_run):
    def responder(argv):
        return (0, "aaa\nbbb")

    rec = install_run(gb, make_git_recorder(responder))
    seeds, note = gb.seed_commits(REPO, "issue", "1234")
    assert seeds == ["aaa", "bbb"]
    assert note is None
    argv = rec.calls[0]
    assert "-E" in argv
    assert "--grep=(^|[^0-9])#1234($|[^0-9])" in argv


def test_seed_commits_mr_warns_when_absent(make_git_recorder, install_run):
    install_run(gb, make_git_recorder(lambda argv: (0, "")))
    seeds, note = gb.seed_commits(REPO, "mr", "42")
    assert seeds == []
    assert note is not None
    assert "!42" in note and "marge-bot" in note


def test_related_commits_expands_provenance_chain_and_subject(
    make_git_recorder, install_run
):
    # aaa is the seed; bbb cherry-picks aaa; ccc cherry-picks bbb (a re-backport
    # chain); ddd shares the subject but lacks the -x trailer.
    def responder(argv):
        if argv[:2] == ["show", "-s"]:
            return (0, "Fix foo")
        if "--grep=cherry picked from commit aaa" in argv:
            return (0, "bbb")
        if "--grep=cherry picked from commit bbb" in argv:
            return (0, "ccc")
        if "--grep=cherry picked from commit ccc" in argv:
            return (0, "")
        if "--grep=Fix foo" in argv:
            return (0, "aaa\nddd")
        return (0, "")

    install_run(gb, make_git_recorder(responder))
    reasons = gb.related_commits(REPO, "sha", ["aaa"])
    assert reasons == {
        "aaa": "seed",
        "bbb": "provenance",
        "ccc": "provenance",
        "ddd": "subject",
    }


def test_related_commits_issue_does_not_expand(make_git_recorder, install_run):
    # An issue ref is preserved across cherry-picks, so the grep already found
    # the backports; we must NOT run provenance/subject expansion (avoids noise).
    rec = install_run(gb, make_git_recorder(lambda argv: (0, "")))
    reasons = gb.related_commits(REPO, "issue", ["aaa", "bbb"])
    assert reasons == {"aaa": "issue-grep", "bbb": "issue-grep"}
    assert rec.calls == []  # no further git calls beyond the (already-done) seed grep
