from __future__ import annotations

import builtins
from importlib.metadata import PackageNotFoundError
import json

import pytest

from tools import check_environment as checker


def package(version="1.0", *requires):
    return {"version": version, "requires": list(requires)}


def lookup_for(installed, calls=None):
    def lookup(name):
        if calls is not None:
            calls.append(name)
        if name not in installed:
            raise PackageNotFoundError(name)
        return installed[name]
    return lookup


def test_reports_missing_and_version_conflicts_without_inspecting_unrelated_packages():
    calls = []
    installed = {"app": package("2.0", "dependency>=3"), "dependency": package("2.0"),
                 "unrelated": package("1.0", "app<1")}
    result = checker.check_dependencies(["app>=2", "missing>=1"], lookup=lookup_for(installed, calls))
    assert not result["ok"]
    assert {issue["kind"] for issue in result["issues"]} == {"missing", "version_mismatch"}
    assert {issue["package"] for issue in result["issues"]} == {"missing", "dependency"}
    assert set(calls) == {"app", "dependency", "missing"}
    assert result["checked_packages"] == 3


def test_extras_expand_after_first_visit_and_transitive_markers_use_target_environment():
    installed = {
        "app": package("1", "base", "later", 'ignored; sys_platform == "linux"'),
        "base": package("1", 'accelerator>=2; extra == "fast-mode"', 'base; extra == "fast-mode"'),
        "later": package("1", "base[fast_mode]", "child[io]"),
        "accelerator": package("2"),
        "child": package("1", 'reader; extra == "io"', 'not-selected; extra == "unused"'),
        "reader": package("1"),
    }
    result = checker.check_dependencies(["app", 'inactive; python_version < "3"'],
        lookup=lookup_for(installed), environment={"sys_platform": "win32", "python_version": "3.11"})
    assert result["ok"]
    assert {row["name"] for row in result["packages"]} == {"app", "base", "later", "accelerator", "child", "reader"}
    assert next(row for row in result["packages"] if row["name"] == "base")["extras"] == ["fast-mode"]


def test_each_active_requirement_constraint_is_checked():
    result = checker.check_dependencies(["app"], lookup=lookup_for({
        "app": package("1", "shared>=2", "other"),
        "other": package("1", "shared<2"), "shared": package("2"),
    }))
    assert result["issues"] == [{"kind": "version_mismatch", "package": "shared",
        "installed": "2", "required": "shared<2", "required_by": "other"}]


@pytest.mark.parametrize("marker", ['extra == ""', 'extra != "fast"'])
def test_extras_keep_base_dependencies_even_when_selected_on_first_visit(marker):
    result = checker.check_dependencies(["app[fast]"], lookup=lookup_for({
        "app": package("1", f"missing-base; {marker}", 'accelerator; extra == "fast"'),
        "accelerator": package("1"),
    }))
    assert not result["ok"]
    assert {issue["package"] for issue in result["issues"]} == {"missing-base"}
    assert {row["name"] for row in result["packages"]} == {"app", "accelerator", "missing-base"}


@pytest.mark.parametrize("bad", [package("not-a-version"), {"version": "1", "requires": "dep"},
    package("1", "bad requirement >= ?"), package("1", 3)])
def test_malformed_installed_metadata_fails_clearly(bad):
    with pytest.raises(checker.EnvironmentCheckError, match="metadata"):
        checker.check_dependencies(["app"], lookup=lookup_for({"app": bad}))


def test_requirements_include_relative_files_comments_and_detect_cycles(tmp_path):
    runtime = tmp_path / "requirements.txt"
    dev = tmp_path / "requirements-dev.txt"
    runtime.write_text("# runtime\napp>=1 # comment\n", encoding="utf-8")
    dev.write_text("-r requirements.txt\npytest>=8\n", encoding="utf-8")
    assert checker.read_requirements(dev) == ["app>=1", "pytest>=8"]
    runtime.write_text("--requirement=requirements-dev.txt\n", encoding="utf-8")
    with pytest.raises(checker.EnvironmentCheckError, match="Cyclic"):
        checker.read_requirements(dev)


@pytest.mark.parametrize("line", ["--index-url https://example.invalid", "-r https://example.invalid/requirements.txt"])
def test_requirements_never_follow_remote_or_pip_options(tmp_path, line):
    path = tmp_path / "requirements.txt"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(checker.EnvironmentCheckError):
        checker.read_requirements(path)


def test_missing_packaging_is_clear_but_does_not_break_help(monkeypatch, capsys):
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "packaging" or name.startswith("packaging."):
            raise ModuleNotFoundError("packaging missing")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(checker.EnvironmentCheckError, match="requires packaging"):
        checker.check_dependencies([])
    monkeypatch.setattr(checker, "read_requirements", lambda *args: pytest.fail("help read requirements"))
    with pytest.raises(SystemExit) as exit_info:
        checker.main(["--help"])
    assert exit_info.value.code == 0
    assert "--dev" in capsys.readouterr().out


def test_workstation_requires_interpreter_and_media_tools_but_fpcalc_is_optional():
    def tools(name):
        return None if name == "fpcalc" else f"/tools/{name}"
    result = checker.check_workstation(executable=checker.REQUIRED_PYTHON, version=(3, 11, 9), which=tools)
    assert result["ok"]
    assert not result["tools"]["fpcalc"]["required"]
    assert not checker.check_workstation(executable="wrong/python.exe", version=(3, 11, 9), which=tools)["ok"]
    assert not checker.check_workstation(executable=checker.REQUIRED_PYTHON, version=(3, 12, 0), which=tools)["ok"]
    assert not checker.check_workstation(executable=checker.REQUIRED_PYTHON, version=(3, 11, 9), which=lambda name: None)["ok"]


def test_cli_json_metadata_failure_has_nonzero_exit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "requirements.txt"
    path.write_text("not a requirement ?", encoding="utf-8")
    assert checker.main(["--requirements", str(path), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"]
    assert "Invalid requirement metadata" in report["error"]
