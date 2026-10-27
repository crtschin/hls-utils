from hls_utils import check_ghc_compat, run_testsuites


def test_rt_cli_wins():
    assert run_testsuites.resolve_targets(["x", "y"]) == ["x", "y"]


def test_rt_env_when_no_cli(monkeypatch):
    monkeypatch.setenv("TEST_TARGETS", "a b c")
    assert run_testsuites.resolve_targets(None) == ["a", "b", "c"]


def test_rt_default_is_all():
    assert run_testsuites.resolve_targets(None) == ["all"]


def test_rt_cli_beats_env(monkeypatch):
    monkeypatch.setenv("TEST_TARGETS", "a b")
    assert run_testsuites.resolve_targets(["x"]) == ["x"]


def test_compat_cli_wins():
    assert check_ghc_compat.resolve_targets(["x"]) == (["x"], [])


def test_compat_env_when_no_cli(monkeypatch):
    monkeypatch.setenv("COMPAT_TARGETS", "a b")
    assert check_ghc_compat.resolve_targets(None) == (["a", "b"], [])


def test_compat_default_disables_tests():
    assert check_ghc_compat.resolve_targets(None) == (["all"], ["--disable-tests"])


def test_default_asymmetry_between_tools():
    rt_targets = run_testsuites.resolve_targets(None)
    _compat_targets, compat_flags = check_ghc_compat.resolve_targets(None)
    assert "--disable-tests" not in rt_targets
    assert "--disable-tests" in compat_flags
