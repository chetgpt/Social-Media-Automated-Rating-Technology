# Source fix validation — 10 September 2026

The source closeout covers dependency declarations, offline startup checks,
and the previously tested publication and registry-repair safeguards.
`comments_data/` remains excluded from routine code review, source inventories,
Git backups, and test inputs. Tests use temporary fixtures.

## Dependency and startup corrections

- Declare `psutil>=7,<8`, required by the existing process supervisors.
- Declare `packaging>=24,<26` for the development environment checker.
- Add `tools/check_environment.py`: inspect only declared runtime/development
  requirements, active transitive package metadata, the required Python, and
  native tools. Extras retain base dependencies. Unrelated installed packages
  do not become workspace failures. Packaging may query local OS version
  information; no workflow, browser, credential, or result-data access occurs.
- Load SONIC numeric feature/evaluation exports on demand. Pure contracts and
  CLI help no longer import NumPy/SciPy. Existing public exports retain identity.
- Correct README startup guidance and distinguish the reserved Mirelo design
  from executable SONIC behavior.

## Workstation result

Required Python 3.11.5, Git, FFmpeg, and FFprobe are present. `fpcalc` is
optional and absent; the existing spectral fallback remains available.

The saved environment had `google-auth` 2.29.0, below the declared minimum,
and no Requests typing stubs. The narrow correction installed:

| Package | Verified version |
| --- | --- |
| google-auth | 2.35.0 |
| types-requests | 2.31.0.6 |
| types-urllib3 | 1.26.25.14 |

These selections satisfy the project requirements and all active installed
reverse constraints for the changed packages. `urllib3` remains 1.26.15;
Playwright, Requests, NumPy/SciPy, and other existing runtime packages were
not upgraded. The previous Google Auth wheel is saved locally for rollback.
All 47 packages in the development dependency graph pass after correction.
The shared global Python installation still has unrelated pre-existing
dependency conflicts; this result does not certify that entire environment.

## Validation

- 19 guarded CLI `--help` entry points passed. The guard blocked database,
  protected runtime/credential path, filesystem mutation, subprocess, and
  outbound network access. It allowed only observed library-local hostname
  and urllib3 IPv6 loopback capability probes. No live operation was run.
- 15 environment-checker tests passed, including transitive constraints,
  markers, extras, missing packages, malformed metadata, and workstation tools.
- 36 Drive archive and publication-worker tests passed using test fixtures.
- 87 SONIC tests passed, including import isolation and public-export identity.
- Mypy passed for the two changed implementation modules.
- Earlier source fixes retain their recorded 1,018-test verification; the
  entire earlier suite was not repeated for this startup closeout.

Detailed environment, resolver, test, and help-check receipts are local-only
under `local_artifacts/environment_check_2026-09-10/`.

## Handoff state

The pre-fix source snapshot was pushed to the approved GitHub backup branch.
The corrective branch is `codex/fix-registry-publication-20260909`, with a
verified local bare Git backup. These corrective commits have not been pushed
to the public origin. The original working branch/index are preserved.

Production registry repair remains paused. Its earlier apply attempt rejected
a stale plan before writing; no production migration is claimed. New result
data does not trigger another scan, repair plan, or source-review restart.
