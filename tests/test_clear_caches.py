"""Tests for hls-clear-caches: which builddirs it evicts and what it spares."""

from hls_utils import clear_caches


def _make_builddir(checkout, name):
    """Create dist-newstyle/<name>/ with a file inside, return the dir path."""
    d = checkout / "dist-newstyle" / name
    d.mkdir(parents=True)
    (d / "cache").write_text("x")
    return d


def test_no_checkout_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no cabal.project here or above
    assert clear_caches.main([]) == 2
    assert "no cabal.project found" in capsys.readouterr().err


def test_nothing_to_clear_returns_0(valid_checkout, capsys):
    assert clear_caches.main([]) == 0
    assert "no compat/test build caches" in capsys.readouterr().out


def test_removes_compat_and_test_spares_others(valid_checkout, capsys):
    compat = _make_builddir(valid_checkout, "compat-ghc96")
    test = _make_builddir(valid_checkout, "test-ghc912")
    own_build = _make_builddir(valid_checkout, "build")  # developer's own state
    logs = valid_checkout / "compat-logs"
    logs.mkdir()
    (logs / "ghc96.log").write_text("log")

    assert clear_caches.main([]) == 0

    assert not compat.exists()
    assert not test.exists()
    assert own_build.exists()  # untouched
    assert logs.exists()  # logs are output, not caches
    out = capsys.readouterr().out
    assert "removed dist-newstyle/compat-ghc96" in out
    assert "removed dist-newstyle/test-ghc912" in out


def test_dry_run_lists_without_deleting(valid_checkout, capsys):
    compat = _make_builddir(valid_checkout, "compat-ghc96")
    assert clear_caches.main(["--dry-run"]) == 0
    assert compat.exists()
    out = capsys.readouterr().out
    assert "would remove dist-newstyle/compat-ghc96" in out
    assert "removed" not in out.replace("would remove", "")


def test_finds_custom_version_builddir(valid_checkout):
    # Glob, not a fixed version list: a custom-named sweep dir is still evicted.
    custom = _make_builddir(valid_checkout, "compat-ghc99")
    assert clear_caches.main([]) == 0
    assert not custom.exists()


def test_ignores_file_sharing_prefix(valid_checkout):
    # A plain file named like the prefix must not be treated as a builddir.
    (valid_checkout / "dist-newstyle").mkdir()
    stray = valid_checkout / "dist-newstyle" / "compat-note.txt"
    stray.write_text("not a builddir")
    assert clear_caches.find_caches(valid_checkout) == []
    assert clear_caches.main([]) == 0
    assert stray.exists()
