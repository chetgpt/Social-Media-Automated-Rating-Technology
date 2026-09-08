from pathlib import Path
import subprocess

import pytest

from social_browser_startup_guard import startup_guard


def test_concurrent_startup_is_bounded_and_lock_releases_after_error(tmp_path):
    with pytest.raises(ValueError):
        with startup_guard(tmp_path):
            with pytest.raises(RuntimeError, match="profile_startup_busy"):
                with startup_guard(tmp_path, timeout=0.01):
                    pytest.fail("Concurrent startup acquired the lock")
            raise ValueError("startup failed")
    with startup_guard(tmp_path, timeout=0):
        pass


def test_bridge_uses_one_monotonic_deadline_without_reset_between_waits():
    # Evaluate only the deadline function from the PowerShell AST, never the
    # helper's browser/UI entry point.
    helper = Path(__file__).resolve().parents[2] / "edge_live_debugging.ps1"
    script = r'''
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw $errors[0].Message }
$f=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-BridgeDeadline'},$true)
Invoke-Expression $f.Extent.Text
$TimeoutSeconds=30
$bridgeClock=[pscustomobject]@{Elapsed=[TimeSpan]::FromSeconds(29)}
if (-not (Test-BridgeDeadline)) { throw 'early timeout' }
$bridgeClock.Elapsed=[TimeSpan]::FromSeconds(30)
if (Test-BridgeDeadline) { throw 'deadline was extended' }
'''
    # Use a file argument to preserve literal PowerShell syntax and exact path.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "check.ps1"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(path), str(helper)],
                                capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
