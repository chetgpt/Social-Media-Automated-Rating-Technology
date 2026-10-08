#!/usr/bin/env python
"""Run AUDIO ARCHIVE sequentially across any or all local project databases.

With no ``--project`` filters this discovers every compatible project beneath
``comments_data``. Re-running skips completed archives and resumes unfinished
ones, so the batch can continue after interruption.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


WORKSPACE_ROOT = Path(__file__).resolve().parent
DEFAULT_COMMENTS_DATA = WORKSPACE_ROOT / "comments_data"
DEFAULT_OUTPUT_ROOT = DEFAULT_COMMENTS_DATA / "audio_archive_runs"
EXCLUDED_PROJECTS = frozenset({"audio_archive_runs", "tiktok_master"})
DELTA_POST_IDS_PER_RUN = 60


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.fspath(Path(path).expanduser().resolve()))


def _open_query_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _candidate_databases(project: Path) -> Iterable[Path]:
    seen: set[str] = set()
    for pattern in ("*.sqlite", "*.sqlite3"):
        for database in sorted(project.rglob(pattern)):
            key = _path_key(database)
            if key not in seen and database.is_file():
                seen.add(key)
                yield database.resolve()


def _discover_runs(
    comments_data: Path,
    selected_projects: list[str],
    selected_run_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if selected_projects:
        projects = []
        for supplied in selected_projects:
            candidate = Path(supplied).expanduser()
            if not candidate.is_absolute():
                candidate = comments_data / candidate
            candidate = candidate.resolve()
            if not candidate.is_dir():
                raise RuntimeError(f"project directory not found: {supplied}")
            projects.append(candidate)
    else:
        projects = [
            project.resolve()
            for project in sorted(comments_data.iterdir())
            if project.is_dir() and project.name not in EXCLUDED_PROJECTS
        ]

    discovered: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for project in projects:
        compatible_database_found = False
        for database in _candidate_databases(project):
            try:
                connection = _open_query_only(database)
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not {"engage_tiktok_runs", "engage_tiktok_posts"}.issubset(tables):
                    connection.close()
                    continue
                compatible_database_found = True
                run_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(engage_tiktok_runs)"
                    )
                }
                select_fields = ["run_id"]
                for field in ("project", "workflow", "status", "created_at"):
                    if field in run_columns:
                        select_fields.append(field)
                rows = connection.execute(
                    "SELECT " + ", ".join(select_fields) + " FROM engage_tiktok_runs "
                    "ORDER BY rowid"
                ).fetchall()
                for row in rows:
                    run = dict(row)
                    run_id = str(run.get("run_id") or "").strip()
                    if not run_id or (selected_run_ids and run_id not in selected_run_ids):
                        continue
                    evidence_ready = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM engage_tiktok_posts "
                            "WHERE run_id=? AND evidence_ready=1",
                            (run_id,),
                        ).fetchone()[0]
                    )
                    if evidence_ready < 1:
                        skipped.append(
                            {
                                "project": project.name,
                                "database": str(database),
                                "run_id": run_id,
                                "reason": "no_evidence_ready_posts",
                            }
                        )
                        continue
                    discovered.append(
                        {
                            "project": str(run.get("project") or project.name),
                            "project_directory": str(project),
                            "database": str(database),
                            "run_id": run_id,
                            "workflow": str(run.get("workflow") or ""),
                            "source_status": str(run.get("status") or ""),
                            "evidence_ready": evidence_ready,
                            "created_at": str(run.get("created_at") or ""),
                        }
                    )
                connection.close()
            except (OSError, sqlite3.Error) as exc:
                skipped.append(
                    {
                        "project": project.name,
                        "database": str(database),
                        "run_id": "",
                        "reason": f"database_unreadable:{type(exc).__name__}",
                    }
                )
        if not compatible_database_found:
            skipped.append(
                {
                    "project": project.name,
                    "database": "",
                    "run_id": "",
                    "reason": "no_compatible_workflow_database",
                }
            )
    discovered.sort(
        key=lambda row: (row["project"].casefold(), row["database"], row["run_id"])
    )
    return discovered, skipped


def _candidate_identity(candidate: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(candidate.get("post_id") or "").strip(),
        str(candidate.get("canonical_url") or candidate.get("url") or "").strip(),
        str(candidate.get("evidence_hash") or "").strip().casefold(),
    )


def _candidate_post_identity(candidate: Mapping[str, Any]) -> tuple[str, str]:
    identity = _candidate_identity(candidate)
    return identity[0], identity[1]


def _project_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _existing_archives(output_root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not output_root.is_dir():
        return found
    for run_directory in sorted(output_root.glob("audio_archive_*")):
        manifest_path = run_directory / "manifest.json"
        state_path = run_directory / "state.json"
        if not manifest_path.is_file() or not state_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            source = manifest["source"]
            selection = manifest.get("selection")
            if not isinstance(selection, list):
                selection = []
            frozen_candidates = [
                dict(candidate)
                for candidate in selection
                if isinstance(candidate, Mapping)
                and all(_candidate_identity(candidate)[:2])
            ]
            found.append({
                "archive_run_id": str(manifest["run_id"]),
                "status": str(state.get("status") or ""),
                "run_directory": str(run_directory.resolve()),
                "source_database": str(source.get("source_database") or ""),
                "local_run_id": str(source.get("local_run_id") or ""),
                "project": str(source.get("project") or ""),
                "terminal": int(state.get("terminal") or 0),
                "selection": frozen_candidates,
                "selection_identities": frozenset(
                    _candidate_identity(candidate) for candidate in frozen_candidates
                ),
                "selection_post_identities": frozenset(
                    _candidate_post_identity(candidate)
                    for candidate in frozen_candidates
                ),
            })
        except (KeyError, OSError, ValueError, TypeError):
            continue
    found.sort(key=lambda row: (row["archive_run_id"], row["run_directory"]))
    return found


def _current_candidates(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    from tiktok_scraper.run_lineage import load_archive_run_posts

    packet = load_archive_run_posts(source["database"], source["run_id"])
    candidates = packet.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [dict(candidate) for candidate in candidates if isinstance(candidate, Mapping)]


def _matching_archives(
    source: Mapping[str, Any],
    current_candidates: Sequence[Mapping[str, Any]],
    existing: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return archives safely bound to the current logical source run.

    An exact path is useful but not sufficient after a database restore or
    replacement. Every match must share at least one frozen post identity.
    A moved database additionally requires the same project identity.
    """

    current_posts = {
        _candidate_post_identity(candidate) for candidate in current_candidates
    }
    source_path = _path_key(source["database"])
    source_project = _project_key(source.get("project"))
    matches: list[dict[str, Any]] = []
    for archive in existing:
        if archive.get("local_run_id") != source.get("run_id"):
            continue
        frozen_posts = set(archive.get("selection_post_identities") or ())
        if not frozen_posts.intersection(current_posts):
            continue
        frozen_path = str(archive.get("source_database") or "")
        exact_path = bool(frozen_path) and _path_key(frozen_path) == source_path
        same_project = bool(source_project) and _project_key(
            archive.get("project")
        ) == source_project
        if exact_path or same_project:
            matches.append(dict(archive))
    matches.sort(
        key=lambda row: (
            row.get("status") != "archive_complete",
            -len(row.get("selection_identities") or ()),
            -int(row.get("terminal") or 0),
            str(row.get("archive_run_id") or ""),
            str(row.get("run_directory") or ""),
        )
    )
    return matches


def _archive_command(
    *,
    output_root: Path,
    source: Mapping[str, Any],
    action: str,
    archive_run_id: str = "",
    post_ids: Sequence[str] = (),
    expected_account: str = "",
) -> list[str]:
    command = [
        sys.executable,
        str(WORKSPACE_ROOT / "audio_archive_tiktok.py"),
        "--output-root",
        str(output_root),
    ]
    if action == "resume":
        command.extend(["resume", "--run-id", archive_run_id])
    else:
        command.extend(
            [
                "run",
                "--source-database",
                str(source["database"]),
                "--source-run-id",
                str(source["run_id"]),
            ]
        )
        for post_id in post_ids:
            command.extend(["--post-id", post_id])
    if expected_account:
        command.extend(["--expected-account", expected_account])
    return command


def _write_batch_report(output_root: Path, report: dict[str, Any]) -> Path:
    batch_root = output_root / "batches"
    batch_root.mkdir(parents=True, exist_ok=True)
    path = batch_root / f"{report['batch_id']}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive evidence-ready TikTok audio from any or all projects."
    )
    parser.add_argument(
        "--comments-data",
        default=str(DEFAULT_COMMENTS_DATA),
        help="Folder containing project directories.",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="AUDIO ARCHIVE output directory.",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Project directory name or path; repeat as needed. Omit for all projects.",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="Optional exact source run filter; repeat as needed.",
    )
    parser.add_argument("--expected-account", default="")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the plan without opening a browser or writing archive output.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop after the first source failure instead of continuing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    comments_data = Path(args.comments_data).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    if not comments_data.is_dir():
        raise SystemExit(f"comments_data directory not found: {comments_data}")

    runs, discovery_skips = _discover_runs(
        comments_data,
        list(args.project),
        set(args.run_id),
    )
    plan = {
        "mode": "selected-projects" if args.project else "all-projects",
        "projects_requested": list(args.project),
        "runs": runs,
        "run_count": len(runs),
        "evidence_ready_total": sum(int(row["evidence_ready"]) for row in runs),
        "discovery_skips": discovery_skips,
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    output_root.mkdir(parents=True, exist_ok=True)
    existing = _existing_archives(output_root)
    batch_id = f"audio_archive_batch_{_timestamp()}"
    results: list[dict[str, Any]] = []
    stop_batch = False
    for index, source in enumerate(runs, start=1):
        potential = [
            archive
            for archive in existing
            if archive.get("local_run_id") == source["run_id"]
        ]
        current_candidates: list[dict[str, Any]] | None = None
        matching: list[dict[str, Any]] = []
        if potential:
            try:
                current_candidates = _current_candidates(source)
            except Exception:
                # Let the single-run command report the source error in its
                # normal form. An unreadable current scope must never inherit
                # completion from an older archive merely because IDs match.
                current_candidates = None
            if current_candidates:
                matching = _matching_archives(
                    source,
                    current_candidates,
                    potential,
                )

        planned_actions: list[dict[str, Any]] = []
        if current_candidates is not None and matching:
            completed_matches = [
                archive
                for archive in matching
                if archive.get("status") == "archive_complete"
            ]
            covered = set().union(
                *(
                    set(archive.get("selection_identities") or ())
                    for archive in completed_matches
                ),
                set(),
            )
            pending = [
                candidate
                for candidate in current_candidates
                if _candidate_identity(candidate) not in covered
            ]
            if not pending:
                primary = completed_matches[0]
                results.append(
                    {
                        **source,
                        "action": "skipped_complete",
                        "outcome": "complete",
                        "archive_run_id": primary["archive_run_id"],
                        "status": primary["status"],
                        "run_directory": primary["run_directory"],
                        "covering_archive_run_ids": [
                            archive["archive_run_id"]
                            for archive in completed_matches
                            if set(archive.get("selection_identities") or ())
                            .intersection(covered)
                        ],
                    }
                )
                continue

            pending_identities = {
                _candidate_identity(candidate) for candidate in pending
            }
            resumable = [
                archive
                for archive in matching
                if archive.get("status")
                in {"planned", "running", "archive_incomplete"}
            ]
            for archive in resumable:
                frozen = set(archive.get("selection_identities") or ())
                if not frozen or not frozen.issubset(pending_identities):
                    continue
                planned_actions.append(
                    {
                        "action": "resume",
                        "archive_run_id": archive["archive_run_id"],
                        "post_ids": [],
                    }
                )
                pending_identities.difference_update(frozen)

            delta_post_ids = [
                str(candidate["post_id"])
                for candidate in pending
                if _candidate_identity(candidate) in pending_identities
            ]
            for offset in range(0, len(delta_post_ids), DELTA_POST_IDS_PER_RUN):
                planned_actions.append(
                    {
                        "action": "run_delta",
                        "archive_run_id": "",
                        "post_ids": delta_post_ids[
                            offset : offset + DELTA_POST_IDS_PER_RUN
                        ],
                    }
                )
        else:
            planned_actions.append(
                {"action": "run", "archive_run_id": "", "post_ids": []}
            )

        action_results: list[dict[str, Any]] = []
        for action_number, planned in enumerate(planned_actions, start=1):
            action = str(planned["action"])
            command = _archive_command(
                output_root=output_root,
                source=source,
                action="resume" if action == "resume" else "run",
                archive_run_id=str(planned.get("archive_run_id") or ""),
                post_ids=list(planned.get("post_ids") or ()),
                expected_account=args.expected_account,
            )
            print(
                f"[{index}/{len(runs)}:{action_number}/{len(planned_actions)}] "
                f"{action}: {source['project']} "
                f"({source['run_id']}, {source['evidence_ready']} posts)",
                flush=True,
            )
            completed = subprocess.run(command, cwd=WORKSPACE_ROOT, check=False)
            action_result = {
                "action": action,
                "archive_run_id": str(planned.get("archive_run_id") or ""),
                "post_ids": list(planned.get("post_ids") or ()),
                "exit_code": int(completed.returncode),
                "outcome": "complete" if completed.returncode == 0 else "failed",
            }
            action_results.append(action_result)
            if completed.returncode != 0:
                stop_batch = bool(args.stop_on_error)
                break

        failed_action = next(
            (row for row in action_results if row["outcome"] == "failed"),
            None,
        )
        result = {
            **source,
            "action": "_then_".join(row["action"] for row in action_results),
            "actions": action_results,
            "exit_code": int(failed_action["exit_code"]) if failed_action else 0,
            "outcome": "failed" if failed_action else "complete",
        }
        results.append(result)
        existing = _existing_archives(output_root)
        if stop_batch:
            break

    report = {
        "schema_version": "tiktok-audio-archive-batch-v1",
        "batch_id": batch_id,
        "mode": plan["mode"],
        "comments_data": str(comments_data),
        "output_root": str(output_root),
        "planned_runs": len(runs),
        "planned_evidence_ready": plan["evidence_ready_total"],
        "completed_or_skipped": sum(
            row.get("outcome") == "complete" or row.get("action") == "skipped_complete"
            for row in results
        ),
        "failed": sum(row.get("outcome") == "failed" for row in results),
        "results": results,
        "discovery_skips": discovery_skips,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    report_path = _write_batch_report(output_root, report)
    report["report_path"] = str(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
