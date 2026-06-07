"""Find which released GHC versions contain a change, including backports.

Given a commit SHA, an issue (``#NNNN``) or a merge request (``!NNNN``), report
the GHC release versions that contain that change. The interesting case is
*backports*: a fix lands on ``master`` then gets cherry-picked (a new SHA) onto
older release branches and ships in a later patch release.

Resolution is fully offline against a local GHC clone (``--repo`` or
``$GHC_REPO``): gitlab.haskell.org's REST API is behind bot-protection, so this
leans on ``git tag/branch --contains`` and ``git log --grep`` instead. A
treeless clone is enough (commit graph + tags, no blobs)::

    git clone --filter=tree:0 --mirror https://gitlab.haskell.org/ghc/ghc.git

Known limits: a later revert is invisible to ``--contains``; ``!MR`` lookup is
heuristic (marge-bot rebases drop the MR number from commit text, so prefer the
SHA); identical commit subjects can collide.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# GHC conventions: release tags ghc-X.Y.Z-release, pre-release ...-alpha/beta/rc,
# stable branches ghc-X.Y (possibly remote-tracking), plus master.
RELEASE_TAG_RE = re.compile(r"^ghc-(\d+)\.(\d+)\.(\d+)-release$")
PRERELEASE_TAG_RE = re.compile(r"^ghc-(\d+)\.(\d+)\.(\d+)-(?:alpha|beta|rc)\d*$")
STABLE_BRANCH_RE = re.compile(r"^(?:remotes/[^/]+/)?ghc-(\d+)\.(\d+)$")
MASTER_BRANCH_RE = re.compile(r"^(?:remotes/[^/]+/)?master$")

ISSUE_RE = re.compile(r"^#(\d+)$")
MR_RE = re.compile(r"^!(\d+)$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class BackportsError(Exception):
    """A fatal, user-facing error. Maps to exit code 2."""


class Version(NamedTuple):
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class CommitRefs(NamedTuple):
    tags: list[str]
    branches: list[str]


# --- git I/O ---------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    """Run ``git -C repo args`` and return stripped stdout.

    Raises BackportsError on a non-zero exit (unknown revision, not a repo).
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BackportsError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def git_lines(repo: Path, *args: str) -> list[str]:
    """git() split into non-empty, stripped lines."""
    return [line.strip() for line in git(repo, *args).splitlines() if line.strip()]


def resolve_repo(repo_arg: str | None) -> Path:
    """Locate the GHC clone: --repo, then $GHC_REPO. Validate it is a git repo."""
    raw = repo_arg or os.environ.get("GHC_REPO")
    if not raw:
        raise BackportsError(
            "no GHC repo given; pass --repo PATH or set GHC_REPO.\n"
            "A lightweight clone is enough:\n"
            "  git clone --filter=tree:0 --mirror "
            "https://gitlab.haskell.org/ghc/ghc.git ~/src/ghc"
        )
    repo = Path(raw).expanduser()
    git(repo, "rev-parse", "--git-dir")  # raises BackportsError if not a repo
    return repo


def grep_all(repo: Path, *grep_args: str) -> list[str]:
    """Full SHAs of commits across all refs matching the given log grep args."""
    return git_lines(repo, "log", "--all", "--format=%H", *grep_args)


def subject_of(repo: Path, sha: str) -> str:
    """The commit's subject line (first line of the message)."""
    return git(repo, "show", "-s", "--format=%s", sha)


def cherry_pick_descendants(repo: Path, seeds: set[str]) -> set[str]:
    """Full SHAs that cherry-picked (``-x``) any seed, transitively.

    ``git cherry-pick -x`` writes ``(cherry picked from commit <full-sha>)``; a
    re-backport references the previous backport, so iterate to a fixpoint.
    """
    known = set(seeds)
    worklist = list(seeds)
    while worklist:
        sha = worklist.pop()
        for hit in grep_all(repo, "-F", f"--grep=cherry picked from commit {sha}"):
            if hit not in known:
                known.add(hit)
                worklist.append(hit)
    return known - set(seeds)


def subject_matches(repo: Path, subject: str) -> set[str]:
    """Full SHAs whose message contains *subject* verbatim (backports without -x)."""
    if not subject:
        return set()
    return set(grep_all(repo, "-F", f"--grep={subject}"))


def seed_commits(repo: Path, kind: str, value: str) -> tuple[list[str], str | None]:
    """Resolve the target to seed full SHAs, plus an optional warning note."""
    if kind == "sha":
        return ([git(repo, "rev-parse", "--verify", f"{value}^{{commit}}")], None)
    # issue / mr: anchor with non-digit boundaries so #1234 does not match #12345.
    sigil = "#" if kind == "issue" else "!"
    pattern = rf"(^|[^0-9]){sigil}{value}($|[^0-9])"
    shas = grep_all(repo, "-E", f"--grep={pattern}")
    note = None
    if not shas and kind == "mr":
        note = (
            f"no commit references !{value}; marge-bot rebases often drop the MR "
            "number from commit text, so try the commit SHA instead."
        )
    return (shas, note)


def related_commits(repo: Path, kind: str, seeds: list[str]) -> dict[str, str]:
    """Map each related full SHA to how it was matched.

    Reasons: ``seed`` (the SHA given), ``issue-grep`` / ``mr-grep`` (found via
    the issue/MR reference), ``provenance`` (cherry-pick ``-x`` trailer),
    ``subject`` (verbatim subject match, a heuristic fallback).
    """
    seed_reason = {"sha": "seed", "issue": "issue-grep", "mr": "mr-grep"}[kind]
    reasons: dict[str, str] = {}
    for sha in seeds:
        reasons.setdefault(sha, seed_reason)
    # For a SHA the backports have *different* SHAs, so expand. An issue ref is
    # preserved across cherry-picks, so its grep already found them; an MR ref is
    # not, so expand there too.
    if kind in ("sha", "mr"):
        for sha in cherry_pick_descendants(repo, set(seeds)):
            reasons.setdefault(sha, "provenance")
        for seed in seeds:
            for sha in subject_matches(repo, subject_of(repo, seed)):
                reasons.setdefault(sha, "subject")
    return reasons


def containing_refs(repo: Path, sha: str) -> CommitRefs:
    """Tags and branches whose history contains *sha*."""
    return CommitRefs(
        tags=git_lines(repo, "tag", "--contains", sha),
        branches=git_lines(repo, "branch", "--all", "--contains", sha),
    )


def gather(repo: Path, reasons: dict[str, str]) -> dict[str, dict]:
    """Fetch subject + containing refs for each related SHA (the I/O boundary)."""
    data: dict[str, dict] = {}
    for sha, reason in reasons.items():
        refs = containing_refs(repo, sha)
        data[sha] = {
            "reason": reason,
            "subject": subject_of(repo, sha),
            "tags": refs.tags,
            "branches": refs.branches,
        }
    return data


# --- pure parsing / coverage ----------------------------------------------


def classify_target(target: str) -> tuple[str, str]:
    """Classify the target as ('issue'|'mr'|'sha', value). Raise on junk."""
    if m := ISSUE_RE.match(target):
        return ("issue", m.group(1))
    if m := MR_RE.match(target):
        return ("mr", m.group(1))
    if SHA_RE.match(target):
        return ("sha", target)
    raise BackportsError(
        f"unrecognized target {target!r}; expected a commit SHA, #ISSUE or !MR"
    )


def parse_release_tag(tag: str) -> Version | None:
    m = RELEASE_TAG_RE.match(tag)
    return Version(int(m[1]), int(m[2]), int(m[3])) if m else None


def prerelease_series(tag: str) -> tuple[int, int] | None:
    """The (major, minor) of a pre-release tag, or None. Keeps the raw tag for
    display: alpha1 vs alpha2 differ only in the suffix we deliberately drop here."""
    m = PRERELEASE_TAG_RE.match(tag)
    return (int(m[1]), int(m[2])) if m else None


def _clean_branch(line: str) -> str:
    return line.strip().removeprefix("* ").strip()


def parse_branch_series(line: str) -> tuple[int, int] | None:
    m = STABLE_BRANCH_RE.match(_clean_branch(line))
    return (int(m[1]), int(m[2])) if m else None


def is_master_branch(line: str) -> bool:
    return bool(MASTER_BRANCH_RE.match(_clean_branch(line)))


def _series_str(series: tuple[int, int]) -> str:
    return f"{series[0]}.{series[1]}"


def compute_coverage(
    target: str, data: dict[str, dict], include_prereleases: bool
) -> dict:
    """Synthesize the report from each commit's refs. Pure, no git."""
    commits: list[dict] = []
    first_release: dict[tuple[int, int], Version] = {}
    prereleases: dict[tuple[int, int], set[str]] = {}
    on_branch: set[tuple[int, int]] = set()
    on_master = False

    for sha, info in data.items():
        releases = [v for t in info["tags"] if (v := parse_release_tag(t)) is not None]
        pretags = [(s, t) for t in info["tags"] if (s := prerelease_series(t))]
        series: set[tuple[int, int]] = set()
        commit_master = False
        for branch in info["branches"]:
            if is_master_branch(branch):
                commit_master = True
            elif (s := parse_branch_series(branch)) is not None:
                series.add(s)
        # Skip a commit that contributes nothing we surface -- e.g. a same-subject
        # match reachable only from a wip branch parses to no release/series/master.
        # Pre-release-only commits count only when we are showing pre-releases.
        if not (
            releases or series or commit_master or (include_prereleases and pretags)
        ):
            continue
        on_master = on_master or commit_master
        on_branch |= series
        for v in releases:
            key = (v.major, v.minor)
            if key not in first_release or v < first_release[key]:
                first_release[key] = v
        for s, t in pretags:
            prereleases.setdefault(s, set()).add(t)

        labels = [f"ghc-{_series_str(s)}" for s in sorted(series)]
        if commit_master:
            labels.append("master")
        commits.append(
            {
                "sha": sha,
                "subject": info["subject"],
                "match_reason": info["reason"],
                "branches": labels,
                "releases": [str(v) for v in sorted(releases)],
            }
        )

    coverage: dict = {
        "released": {
            _series_str(k): str(first_release[k]) for k in sorted(first_release)
        },
        "unreleased_series": [
            _series_str(s) for s in sorted(on_branch) if s not in first_release
        ],
        "on_master": on_master,
    }
    if include_prereleases:
        coverage["prereleases"] = {
            _series_str(k): sorted(prereleases[k]) for k in sorted(prereleases)
        }
    return {"target": target, "commits": commits, "coverage": coverage}


# --- output ----------------------------------------------------------------


def print_human(report: dict) -> None:
    cov = report["coverage"]
    print(f"target: {report['target']}")
    if cov["released"]:
        print("released GHCs containing this change (first release per series):")
        for series, ver in cov["released"].items():
            print(f"  {series}: {ver}+")
    else:
        print("not in any GHC release tag.")
    if cov["unreleased_series"]:
        print("merged but not yet released on: " + ", ".join(cov["unreleased_series"]))
    if cov["on_master"]:
        print("present on master (will ship in the next series).")
    for series, vers in cov.get("prereleases", {}).items():
        print(f"  pre-release {series}: {', '.join(vers)}")
    print(f"commits ({len(report['commits'])}):")
    for c in report["commits"]:
        print(f"  {c['sha'][:12]} [{c['match_reason']}] {c['subject']}")
        print(f"      branches: {', '.join(c['branches']) or '-'}")
        print(f"      releases: {', '.join(c['releases']) or '-'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hls-find-ghc-backports",
        description=(
            "Report which released GHC versions contain a change (including "
            "backports), given a commit SHA, #ISSUE or !MR. Offline against a "
            "local GHC clone."
        ),
        epilog=(
            "Known limits: reverts are invisible to --contains; !MR lookup is "
            "heuristic (prefer the SHA); identical subjects can collide."
        ),
    )
    parser.add_argument("target", help="commit SHA, #ISSUE or !MR (quote # and !).")
    parser.add_argument("--repo", help="path to a GHC git clone (else $GHC_REPO).")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON.")
    parser.add_argument(
        "--include-prereleases",
        action="store_true",
        help="also report alpha/beta/rc tags.",
    )
    args = parser.parse_args(argv)

    try:
        repo = resolve_repo(args.repo)
        kind, value = classify_target(args.target)
        seeds, note = seed_commits(repo, kind, value)
        if note:
            print(f"note: {note}", file=sys.stderr)
        if not seeds:
            print(f"no commits found for {args.target}", file=sys.stderr)
            return 1
        reasons = related_commits(repo, kind, seeds)
        report = compute_coverage(
            args.target, gather(repo, reasons), args.include_prereleases
        )
    except BackportsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
