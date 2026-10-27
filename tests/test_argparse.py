import pytest

from hls_utils import check_ghc_compat, run_testsuites


def test_rt_requires_version():
    with pytest.raises(SystemExit) as exc:
        run_testsuites.main([])
    assert exc.value.code == 2


def test_rt_rejects_extra_positional():
    with pytest.raises(SystemExit) as exc:
        run_testsuites.main(["a", "b"])
    assert exc.value.code == 2


def test_compat_tolerates_zero_versions(tmp_path, monkeypatch):
    # Exit code 2 comes from the missing cabal.project, after argparse accepted
    # the empty list.
    monkeypatch.chdir(tmp_path)
    assert check_ghc_compat.main([]) == 2


def test_rt_targets_requires_value():
    with pytest.raises(SystemExit) as exc:
        run_testsuites.main(["ghc96", "-t"])
    assert exc.value.code == 2


def test_compat_targets_requires_value():
    with pytest.raises(SystemExit) as exc:
        check_ghc_compat.main(["ghc96", "-t"])
    assert exc.value.code == 2
