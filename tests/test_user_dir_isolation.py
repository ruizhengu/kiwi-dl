"""No test may touch the real user data directory.

This is not hypothetical. `tests/test_session_lifecycle_failures.py` drove
`account add` through the CLI with `tmp_path` passed only as `download_path`,
so `accounts_file()` resolved to the OPERATOR'S vault and a
`user@example.invalid` credential was written into it - twice, months apart.

The damage is not the junk row. A test credential is encrypted under the test's
own passphrase, so it is a perfectly readable entry that the operator's
passphrase cannot open: `account add` then refuses every real account, because
the vault it is checking against genuinely does hold a credential under a
different passphrase. The user is told their passphrase is wrong, and it is -
for a row they never created.

Patching that one call site would not have been the fix. Nothing stopped the
next test from doing the same, which is why the guard is an autouse fixture in
`conftest.py` and why this file asserts the guard itself is in place.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiwi_dl.config import config_file, data_dir, user_dir


def _repo_user_dir() -> Path:
    """Where the real store lives when the environment says nothing."""
    return Path(__file__).resolve().parent.parent / "User"


def test_the_user_dir_env_var_is_always_set_during_a_test():
    value = os.environ.get("KIWI_DL_HOME")
    assert value, "the autouse isolation fixture in conftest.py is not active"
    assert _repo_user_dir() not in Path(value).resolve().parents
    assert Path(value).resolve() != _repo_user_dir()


def test_every_resolved_path_stays_out_of_the_real_user_dir():
    real = _repo_user_dir()
    for resolver in (user_dir, data_dir, config_file):
        resolved = Path(resolver()).resolve()
        assert (
            real != resolved and real not in resolved.parents
        ), f"{resolver.__name__}() resolves inside the real user dir: {resolved}"
