"""Check declared dependencies and workstation tools without running workflows.

Only requirements files, installed distribution metadata, the running Python,
and local platform/tool information are inspected. Checked packages are never
imported. No browser, network, collected results, or operational database is
accessed. Packaging may query the local operating-system version.
"""
from __future__ import annotations

import argparse
from collections import deque
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PYTHON = r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
SCHEMA = "tiktok-workspace-environment-v1"


class EnvironmentCheckError(ValueError):
    """Input or installed metadata cannot be checked reliably."""


def _packaging():
    # Lazy loading keeps --help available even before development setup.
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
        from packaging.version import Version
    except ImportError as exc:
        raise EnvironmentCheckError(
            "The checker requires packaging; install requirements-dev.txt "
            "with the required Python interpreter."
        ) from exc
    return Requirement, Version, canonicalize_name, default_environment


def read_requirements(path: Path, stack: tuple[Path, ...] = ()) -> list[str]:
    """Read plain PEP 508 entries and relative -r includes; reject pip options."""
    path = Path(path).resolve()
    if path in stack:
        raise EnvironmentCheckError(f"Cyclic requirements include: {path.name}")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EnvironmentCheckError(f"Cannot read requirements file: {path}") from exc
    result: list[str] = []
    for number, raw in enumerate(lines, 1):
        line = re.split(r"\s+#", raw.strip(), maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        include = re.fullmatch(r"(?:-r\s*|--requirement(?:\s+|=))(.+)", line)
        if include:
            included = include.group(1).strip().strip("\"'")
            if "://" in included:
                raise EnvironmentCheckError("Remote requirements includes are unsupported")
            result.extend(read_requirements(path.parent / included, (*stack, path)))
        elif line.startswith("-") or line.endswith("\\"):
            raise EnvironmentCheckError(f"Unsupported requirements syntax: {path.name}:{number}")
        else:
            result.append(line)
    return result


def distribution_metadata(name: str) -> dict[str, Any]:
    distribution = metadata.distribution(name)
    return {"version": distribution.version, "requires": distribution.requires or []}


def check_dependencies(
    requirements: Sequence[str],
    *,
    lookup: Callable[[str], Mapping[str, Any]] = distribution_metadata,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Follow only active dependencies, revisiting nodes when extras expand."""
    Requirement, Version, canonicalize_name, default_environment = _packaging()
    marker_environment = {**default_environment(), **dict(environment or {})}
    packages: dict[str, dict[str, Any]] = {}
    pending: deque[str] = deque()
    edges: dict[tuple[str, str], Any] = {}

    def parse(raw: Any, owner: str):
        if not isinstance(raw, str):
            raise EnvironmentCheckError(f"Invalid requirement metadata for {owner}")
        try:
            requirement = Requirement(raw)
        except Exception as exc:
            raise EnvironmentCheckError(f"Invalid requirement metadata for {owner}") from exc
        if requirement.url:
            raise EnvironmentCheckError(f"Direct URL requirements are unsupported: {owner}")
        return requirement

    def active(requirement, extras: set[str]) -> bool:
        if requirement.marker is None:
            return True
        try:
            return any(
                requirement.marker.evaluate({**marker_environment, "extra": extra})
                for extra in ({""} | extras)
            )
        except Exception as exc:
            raise EnvironmentCheckError("Cannot evaluate a dependency marker") from exc

    def add(requirement, owner: str) -> None:
        name = canonicalize_name(requirement.name)
        edges[(owner, str(requirement))] = requirement
        extras = {canonicalize_name(extra) for extra in requirement.extras}
        if name not in packages:
            packages[name] = {"extras": extras, "loaded": False, "version": None, "requires": []}
            pending.append(name)
        elif extras - packages[name]["extras"]:
            packages[name]["extras"].update(extras)
            pending.append(name)

    for raw in requirements:
        requirement = parse(raw, "requirements file")
        if active(requirement, set()):
            add(requirement, "requirements")

    while pending:
        name = pending.popleft()
        package = packages[name]
        if not package["loaded"]:
            try:
                installed = lookup(name)
            except metadata.PackageNotFoundError:
                package["loaded"] = True
                continue
            except Exception as exc:
                raise EnvironmentCheckError(f"Cannot read installed metadata for {name}") from exc
            try:
                package["version"] = str(Version(installed["version"]))
                raw_requires = installed.get("requires") or []
                if not isinstance(raw_requires, (list, tuple)):
                    raise ValueError("requires must be a list")
                package["requires"] = [parse(raw, name) for raw in raw_requires]
            except EnvironmentCheckError:
                raise
            except Exception as exc:
                raise EnvironmentCheckError(f"Invalid installed metadata for {name}") from exc
            package["loaded"] = True
        for requirement in package["requires"]:
            if active(requirement, package["extras"]):
                add(requirement, name)

    issues = []
    required_by: dict[str, set[str]] = {name: set() for name in packages}
    for (owner, raw), requirement in sorted(edges.items()):
        name = canonicalize_name(requirement.name)
        installed = packages[name]["version"]
        required_by[name].add(owner)
        if installed is None or not requirement.specifier.contains(installed):
            issues.append({
                "kind": "missing" if installed is None else "version_mismatch",
                "package": name,
                "installed": installed,
                "required": raw,
                "required_by": owner,
            })
    return {
        "ok": not issues,
        "checked_packages": len(packages),
        "packages": [
            {"name": name, "version": package["version"],
             "extras": sorted(package["extras"]), "required_by": sorted(required_by[name])}
            for name, package in sorted(packages.items())
        ],
        "issues": issues,
    }


def check_workstation(
    *,
    executable: str = sys.executable,
    version: Sequence[int] = sys.version_info[:3],
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    same_path = os.path.normcase(os.path.abspath(executable)) == os.path.normcase(
        os.path.abspath(REQUIRED_PYTHON)
    )
    python = {
        "ok": same_path and tuple(version[:2]) == (3, 11),
        "executable": executable,
        "version": ".".join(map(str, version)),
        "required_executable": REQUIRED_PYTHON,
    }
    tools = {
        name: {"required": name != "fpcalc", "path": which(name)}
        for name in ("git", "ffmpeg", "ffprobe", "fpcalc")
    }
    return {
        "ok": python["ok"] and all(item["path"] or not item["required"] for item in tools.values()),
        "python": python,
        "tools": tools,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dev", action="store_true", help="Check requirements-dev.txt including runtime requirements")
    source.add_argument("--requirements", type=Path, help="Check an explicit local requirements file")
    parser.add_argument("--json", action="store_true", help="Emit structured results")
    args = parser.parse_args(argv)
    path = args.requirements or ROOT / ("requirements-dev.txt" if args.dev else "requirements.txt")
    try:
        dependencies = check_dependencies(read_requirements(path))
        workstation = check_workstation()
        report = {"schema": SCHEMA, "ok": dependencies["ok"] and workstation["ok"],
                  "requirements_file": str(path.resolve()), "dependencies": dependencies,
                  "workstation": workstation}
    except EnvironmentCheckError as exc:
        report = {"schema": SCHEMA, "ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    elif "error" in report:
        print(f"Environment check failed: {report['error']}")
    else:
        print(f"Environment: {'PASS' if report['ok'] else 'NEEDS ATTENTION'}")
        print(f"Checked {dependencies['checked_packages']} active dependency packages")
        for issue in dependencies["issues"]:
            print(f"{issue['kind']}: {issue['required']} (installed {issue['installed'] or 'none'}; from {issue['required_by']})")
        python = workstation["python"]
        print(f"Python: {'PASS' if python['ok'] else 'MISMATCH'} {python['version']} at {python['executable']}")
        for name, tool in workstation["tools"].items():
            print(f"{name}: {tool['path'] or ('MISSING' if tool['required'] else 'optional; spectral fallback available')}")
    return 2 if "error" in report else (0 if report["ok"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
