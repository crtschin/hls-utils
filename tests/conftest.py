"""Fixtures for a hermetic suite. No test runs a real cabal, GHC or git."""

import pytest

# Add every env var that the tools read, or it leaks in from the environment.
_ENV_VARS = ("HLS_GHCS", "HLS_GHCS_FILE", "TEST_TARGETS", "COMPAT_TARGETS", "GHC_REPO")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def temp_sweep_logs(tmp_path, monkeypatch):
    def fake(name):
        directory = tmp_path / "sweep-logs" / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    for module in ("hls_utils.check_ghc_compat", "hls_utils.run_testsuites"):
        monkeypatch.setattr(f"{module}.sweep_log_dir", fake)


@pytest.fixture
def valid_checkout(tmp_path, monkeypatch):
    (tmp_path / "cabal.project").write_text("packages: .\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    """subprocess.run stand-in. The ``--numeric-version`` probe always succeeds.

    Every other call takes the next of *returncodes*, then 0 once they run out.
    """

    def __init__(self, returncodes=None, probe_version="9.6.7"):
        self.calls = []
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
        return [argv for argv, _ in self.calls if argv[-1] != "--numeric-version"]


@pytest.fixture
def make_recorder():
    return Recorder


class GitRecorder:
    """subprocess.run stand-in that answers each git call with *responder*."""

    def __init__(self, responder):
        self.calls = []  # list of git argv (without `git -C <repo>`)
        self._responder = responder

    def __call__(self, cmd, *args, **kwargs):
        argv = list(cmd)
        git_args = argv[3:] if argv[:1] == ["git"] and argv[1:2] == ["-C"] else argv
        self.calls.append(git_args)
        rc, out = self._responder(git_args)
        return FakeCompleted(rc, out)


@pytest.fixture
def make_git_recorder():
    return GitRecorder


@pytest.fixture
def install_run(monkeypatch):
    def _install(module, recorder):
        monkeypatch.setattr(module.subprocess, "run", recorder)
        return recorder

    return _install
