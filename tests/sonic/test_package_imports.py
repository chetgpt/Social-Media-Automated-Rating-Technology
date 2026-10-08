"""Keep SONIC's pure contracts and CLI help independent of acoustic libraries."""
from pathlib import Path
import subprocess
import sys

import pytest


def test_contracts_and_help_work_without_numeric_packages():
    workspace = Path(__file__).resolve().parents[2]
    script = r'''
import builtins
import runpy
import sys
original_import = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split(".")[0] in {"numpy", "scipy", "soundfile"}:
        raise AssertionError("Help must not import numeric packages: " + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guard
import sonic_audit.contracts
import sonic_audit.corpus
assert "sonic_audit.features" not in sys.modules
assert "sonic_audit.evaluation" not in sys.modules
sys.argv = ["sonic_audit_tiktok.py", "--help"]
runpy.run_path("sonic_audit_tiktok.py", run_name="__main__")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=workspace,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_existing_package_exports_preserve_identity():
    import sonic_audit
    from sonic_audit import evaluation, features

    aliases = {
        "STATISTICAL_VALIDATION_ALGORITHM_VERSION": "ALGORITHM_VERSION",
        "STATISTICAL_VALIDATION_SCHEMA_VERSION": "SCHEMA_VERSION",
        "build_statistical_validation": "build_statistical_validation",
    }
    for name in sonic_audit.__all__:
        source = evaluation if name in aliases else features
        assert getattr(sonic_audit, name) is getattr(source, aliases.get(name, name))
        assert name in dir(sonic_audit)
    with pytest.raises(AttributeError):
        getattr(sonic_audit, "missing_public_export")
