"""Tests for compute_coverage: the pure synthesis from per-commit refs (no git)."""

from hls_utils import ghc_backports as gb


def _commit(reason="seed", subject="Fix foo", tags=(), branches=()):
    return {
        "reason": reason,
        "subject": subject,
        "tags": list(tags),
        "branches": list(branches),
    }


def test_backport_first_release_per_series_numeric_min():
    # Original on master + 9.12/9.14; backport (different SHA) on 9.10.
    data = {
        "aaa": _commit(
            tags=["ghc-9.12.1-release", "ghc-9.14.1-release"],
            branches=[
                "* master",
                "  remotes/origin/ghc-9.12",
                "  remotes/origin/ghc-9.14",
            ],
        ),
        "bbb": _commit(
            reason="provenance",
            tags=["ghc-9.10.4-release", "ghc-9.10.3-release"],
            branches=["  remotes/origin/ghc-9.10"],
        ),
    }
    cov = gb.compute_coverage("aaa", data, include_prereleases=False)["coverage"]
    # Numeric order, and 9.10's first release is the min patch (9.10.3).
    assert list(cov["released"].items()) == [
        ("9.10", "9.10.3"),
        ("9.12", "9.12.1"),
        ("9.14", "9.14.1"),
    ]
    assert cov["unreleased_series"] == []
    assert cov["on_master"] is True
    assert "prereleases" not in cov


def test_master_only_unreleased():
    data = {"aaa": _commit(branches=["* master"])}
    cov = gb.compute_coverage("aaa", data, include_prereleases=False)["coverage"]
    assert cov["released"] == {}
    assert cov["on_master"] is True
    assert cov["unreleased_series"] == []


def test_merged_to_stable_but_not_yet_released():
    data = {"aaa": _commit(branches=["  remotes/origin/ghc-9.12"])}
    cov = gb.compute_coverage("aaa", data, include_prereleases=False)["coverage"]
    assert cov["released"] == {}
    assert cov["unreleased_series"] == ["9.12"]
    assert cov["on_master"] is False


def test_prereleases_hidden_by_default_shown_on_flag():
    data = {
        "aaa": _commit(
            tags=["ghc-9.14.1-alpha1", "ghc-9.14.1-alpha2", "ghc-9.12.1-release"],
            branches=["* master"],
        )
    }
    off = gb.compute_coverage("aaa", data, include_prereleases=False)["coverage"]
    assert "prereleases" not in off
    # alpha1 and alpha2 are distinct tags, not collapsed.
    on = gb.compute_coverage("aaa", data, include_prereleases=True)["coverage"]
    assert on["prereleases"] == {"9.14": ["ghc-9.14.1-alpha1", "ghc-9.14.1-alpha2"]}
    # The release series is still excluded from prereleases / kept in released.
    assert on["released"] == {"9.12": "9.12.1"}


def test_commit_in_no_ref_is_dropped():
    # A same-subject match reachable from no branch/tag tells us nothing.
    data = {
        "aaa": _commit(tags=["ghc-9.12.1-release"], branches=["* master"]),
        "ddd": _commit(reason="subject"),  # no tags, no branches
    }
    report = gb.compute_coverage("aaa", data, include_prereleases=False)
    assert [c["sha"] for c in report["commits"]] == ["aaa"]
    assert report["coverage"]["released"] == {"9.12": "9.12.1"}


def test_commit_record_shape():
    data = {
        "aaa": _commit(
            subject="Fix the thing",
            tags=["ghc-9.12.1-release"],
            branches=["* master", "  remotes/origin/ghc-9.12"],
        )
    }
    report = gb.compute_coverage("#9", data, include_prereleases=False)
    assert report["target"] == "#9"
    (c,) = report["commits"]
    assert c["sha"] == "aaa"
    assert c["subject"] == "Fix the thing"
    assert c["match_reason"] == "seed"
    assert c["branches"] == ["ghc-9.12", "master"]
    assert c["releases"] == ["9.12.1"]
