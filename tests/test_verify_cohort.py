"""verify_cohort's failure paths must name the actual cause.

Both of these regressed the same way once: the real error was swallowed and
the operator saw a subprocess traceback naming argv instead of the missing
dependency. These tests pin the readable form so it cannot silently revert.

`verify_cohort.py` lives in data/ingestion alongside modules whose names start
with digits, so it is loaded by path rather than imported as a package member.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "data" / "ingestion" / "verify_cohort.py"


def load_verify_cohort():
    spec = importlib.util.spec_from_file_location("verify_cohort", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vc():
    return load_verify_cohort()


def test_generator_failure_reports_child_stderr(vc, tmp_path, monkeypatch):
    """A non-zero generator exit surfaces what the child printed.

    subprocess.run(check=True) would raise CalledProcessError, whose message is
    the command line — the captured stderr, which holds the only useful text,
    is not shown at all.
    """
    boom = tmp_path / "boom.py"
    boom.write_text("import sys; sys.stderr.write('pyarrow is required\\n'); sys.exit(3)")
    monkeypatch.setattr(vc, "GENERATOR", boom)

    with pytest.raises(SystemExit) as exc:
        vc.run_generator(tmp_path / "out")

    message = str(exc.value)
    assert "pyarrow is required" in message, "child stderr must reach the operator"
    assert "exited 3" in message, "the exit status identifies which failure this was"


def test_missing_parquet_engine_names_the_install_command(vc, monkeypatch):
    """Preflight fails with the fix, not with pandas' import error."""
    monkeypatch.setattr(
        vc.importlib.util, "find_spec", lambda name: None if name in {"pyarrow", "fastparquet"} else object()
    )

    with pytest.raises(SystemExit) as exc:
        vc.require_parquet_engine()

    assert "requirements-dev.txt" in str(exc.value)


def test_parquet_engine_present_is_a_no_op(vc):
    """The happy path must not exit — pyarrow is pinned in requirements-dev."""
    if importlib.util.find_spec("pyarrow") is None and importlib.util.find_spec("fastparquet") is None:
        pytest.skip("no parquet engine installed in this environment")
    assert vc.require_parquet_engine() is None


def test_module_path_assumption_holds():
    """Guards the by-path load above against a file move."""
    assert MODULE_PATH.is_file(), f"expected verify_cohort.py at {MODULE_PATH}"
