"""Tests for the pure target/tag/branch parsing in hls_utils.ghc_backports."""

import pytest

from hls_utils import ghc_backports as gb
from hls_utils.ghc_backports import BackportsError, Version

# --- classify_target ---


@pytest.mark.parametrize(
    "target,expected",
    [
        ("#1234", ("issue", "1234")),
        ("!42", ("mr", "42")),
        ("abc1234", ("sha", "abc1234")),
        ("d" * 40, ("sha", "d" * 40)),
        ("1234567", ("sha", "1234567")),  # 7+ digits read as a short SHA
    ],
)
def test_classify_target_ok(target, expected):
    assert gb.classify_target(target) == expected


@pytest.mark.parametrize("target", ["", "nope!", "12.3", "123456", "#", "!"])
def test_classify_target_rejects(target):
    with pytest.raises(BackportsError):
        gb.classify_target(target)


# --- parse_release_tag ---


def test_parse_release_tag():
    assert gb.parse_release_tag("ghc-9.10.2-release") == Version(9, 10, 2)


@pytest.mark.parametrize(
    "tag", ["ghc-9.10.1-alpha1", "ghc-9.6", "ghc-9.10.2", "nonsense"]
)
def test_parse_release_tag_rejects(tag):
    assert gb.parse_release_tag(tag) is None


# --- prerelease_series ---


@pytest.mark.parametrize(
    "tag,series",
    [
        ("ghc-9.10.1-alpha1", (9, 10)),
        ("ghc-9.12.1-rc2", (9, 12)),
        ("ghc-9.14.1-beta1", (9, 14)),
    ],
)
def test_prerelease_series(tag, series):
    assert gb.prerelease_series(tag) == series


def test_prerelease_series_rejects_release():
    assert gb.prerelease_series("ghc-9.10.2-release") is None


# --- branch parsing ---


@pytest.mark.parametrize(
    "line,series",
    [
        ("  remotes/origin/ghc-9.10", (9, 10)),
        ("* ghc-9.8", (9, 8)),
        ("  ghc-9.14", (9, 14)),
    ],
)
def test_parse_branch_series(line, series):
    assert gb.parse_branch_series(line) == series


@pytest.mark.parametrize(
    "line",
    ["  master", "  remotes/origin/HEAD -> origin/master", "  wip/foo"],
)
def test_parse_branch_series_none(line):
    assert gb.parse_branch_series(line) is None


@pytest.mark.parametrize("line", ["* master", "  remotes/origin/master", "master"])
def test_is_master_branch(line):
    assert gb.is_master_branch(line)


@pytest.mark.parametrize(
    "line", ["  ghc-9.10", "  remotes/origin/HEAD -> origin/master"]
)
def test_is_master_branch_false(line):
    assert not gb.is_master_branch(line)


# --- Version ordering / display ---


def test_version_sorts_numerically_not_lexically():
    assert sorted([Version(9, 10, 1), Version(9, 8, 4)]) == [
        Version(9, 8, 4),
        Version(9, 10, 1),
    ]


def test_version_str():
    assert str(Version(9, 10, 2)) == "9.10.2"
