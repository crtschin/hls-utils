"""Shared fixtures for the hls-utils test suite.

The suite is hermetic: it never runs real cabal/GHC. ``subprocess.run`` is
replaced by a Recorder that records argv and returns canned results, so tests
assert the commands the tools *would* run and their control flow.
"""

import pytest

# Every env var the tools read; cleared before each test to avoid leakage.
_ENV_VARS = ("HLS_GHCS", "HLS_GHCS_FILE", "TEST_TARGETS", "COMPAT_TARGETS", "GHC_REPO")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip every env var the tools read, so each test starts from a known state."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def valid_checkout(tmp_path, monkeypatch):
    """A tmp dir that looks like an HLS checkout (has cabal.project), cwd'd into."""
    (tmp_path / "cabal.project").write_text("packages: .\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    """Replacement for subprocess.run that records calls and returns canned results.

    The ``ghc --numeric-version`` probe (argv ending in ``--numeric-version``)
    always succeeds with *probe_version*. Other calls (cabal build/test) consume
    *returncodes* in order, defaulting to 0 once exhausted.
    """

    def __init__(self, returncodes=None, probe_version="9.6.7"):
        self.calls = []  # list of (argv, kwargs)
        self.probe_version = probe_version
        self._rcs = list(returncodes or [])

    def __call__(self, cmd, *args, **kwargs):
        argv = list(cmd)
        self.calls.append((argv, kwargs))
        if argv[-1] == "--numeric-version":
            return FakeCompleted(0, self.probe_version + "\n")
        rc = self._rcs.pop(0) if self._rcs else 0
        return FakeCompleted(rc)

    @property
    def cabal_calls(self):
        """argv of every recorded call except the numeric-version probe."""
        return [argv for argv, _ in self.calls if argv[-1] != "--numeric-version"]


@pytest.fixture
def make_recorder():
    """Factory for Recorder instances (e.g. ``make_recorder(returncodes=[0, 1])``)."""
    return Recorder


class GitRecorder:
    """subprocess.run stand-in for git tools: dispatch on argv via *responder*.

    ``responder(argv) -> (returncode, stdout)`` lets a test return canned output
    per git subcommand (rev-parse, log --grep, tag/branch --contains, show).
    Recorded ``calls`` are the git argv (the leading ``git -C <repo>`` stripped).
    """

    def __init__(self, responder):
        self.calls = []  # list of git argv (without `git -C <repo>`)
        self._responder = responder

    def __call__(self, cmd, *args, **kwargs):
        argv = list(cmd)
        # Drop the `git -C <repo>` prefix so matchers see just the subcommand.
        git_args = argv[3:] if argv[:1] == ["git"] and argv[1:2] == ["-C"] else argv
        self.calls.append(git_args)
        rc, out = self._responder(git_args)
        return FakeCompleted(rc, out)


@pytest.fixture
def make_git_recorder():
    """Factory for GitRecorder (e.g. ``make_git_recorder(responder)``)."""
    return GitRecorder


@pytest.fixture
def install_run(monkeypatch):
    """Patch *module*'s subprocess.run with *recorder*; returns the recorder."""

    def _install(module, recorder):
        monkeypatch.setattr(module.subprocess, "run", recorder)
        return recorder

    return _install
