from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

import engage_tiktok
import tiktok_one_top_content as runner
from tiktok_scraper.top_content_state import TopContentLinkState


SOURCE_URL = (
    "https://ads.tiktok.com/creative/forpartners/creator/top-content?region=row"
)


def _video_id(ordinal: int) -> str:
    return str(7_670_000_000_000_000_000 + ordinal)


def _video_url(ordinal: int) -> str:
    return f"https://www.tiktok.com/@creator{ordinal}/video/{_video_id(ordinal)}"


def _discovery(count: int, *, excluded: int = 0) -> dict[str, Any]:
    links: list[dict[str, Any]] = []
    ranking_observations: list[dict[str, Any]] = []
    for ordinal in range(1, count + 1):
        if ordinal <= 100:
            ranking = "video_views"
            ranking_position = ordinal
        elif ordinal <= 200:
            ranking = "engagement"
            ranking_position = ordinal - 100
        else:
            ranking = "six_second_views"
            ranking_position = ordinal - 200
        links.append(
            {
                "ordinal": ordinal,
                "video_id": _video_id(ordinal),
                "url": _video_url(ordinal) + "?lang=en#discarded",
                "creator": f"@CREATOR{ordinal}",
                "ranking": ranking,
                "ranking_position": ranking_position,
            }
        )
    for ranking in runner.DEFAULT_RANKINGS:
        ranking_observations.append(
            {
                "ranking": ranking,
                "traversed_cards": min(100, count),
                "excluded_video_ids": excluded if ranking == "video_views" else 0,
                "duplicate_links": 0,
                "ranking_exhausted": count > 100,
            }
        )
    return {
        "source_url": SOURCE_URL,
        "region": "row",
        "filter_snapshot": {
            "country": "Indonesia",
            "period": "Last 30 days",
            "organic_only": True,
        },
        "list_snapshot": {
            "requested_count": count,
            "rankings_requested": list(runner.DEFAULT_RANKINGS),
            "ranking_observations": ranking_observations,
            "unique_links_found": count,
            "excluded_video_ids": excluded,
            "duplicate_video_ids": 0,
        },
        "account_binding_hash": "a" * 64,
        "links": links,
        "rankings_exhausted": [],
        "frontier_verified": True,
        "reasons": [],
    }


def _make_complete_parent(
    database: Path, *, run_id: str = "parent-run", count: int = 1
) -> None:
    with TopContentLinkState(database) as state:
        state.create_run(
            run_id,
            source_url=SOURCE_URL,
            requested_count=count,
            scope={"source_mode": "tiktok_one_top_content_links", "region": "row"},
            filter_snapshot={"country": "Indonesia", "period": "Last 30 days"},
            list_snapshot={"requested_count": count, "unique_links_found": count},
            account_binding={"schema_version": "test-v1", "sha256": "a" * 64},
            ranking_order=list(runner.DEFAULT_RANKINGS),
        )
        for ordinal in range(1, count + 1):
            ranking = "video_views" if ordinal <= 100 else "engagement"
            ranking_position = ordinal if ordinal <= 100 else ordinal - 100
            state.checkpoint_link(
                run_id,
                ordinal=ordinal,
                video_id=_video_id(ordinal),
                canonical_url=_video_url(ordinal),
                creator=f"creator{ordinal}",
                ranking_key=ranking,
                ranking_position=ranking_position,
            )
        assert state.finalize(run_id)["status"] == "links_complete"


def _known_snapshot(
    ids: set[str], master_database: Path
) -> tuple[set[str], dict[str, Any]]:
    return ids, {
        "schema_version": runner.KNOWN_SET_SCHEMA,
        "database": str(master_database.resolve()),
        "known_post_count": len(ids),
        "known_post_ids_hash": runner.canonical_sha256(sorted(ids)),
        "observed_at": "2026-08-22T00:00:00+00:00",
    }


def _write_child_run(
    child_database: Path,
    *,
    run_id: str,
    project: str,
    canonical_url: str,
    master_database: Path,
    status: str,
    evidence_ready: int,
    observed_account: str = "",
) -> None:
    child_database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(child_database)
    try:
        engage_tiktok.ensure_schema(connection)
        engage_tiktok.create_run(
            connection,
            project=project,
            topic="",
            requested_count=1,
            max_comments=100,
            max_pages=1,
            workflow="listen",
            source_mode="url",
            direct_post_url=canonical_url,
            master_database=str(master_database.resolve()),
            run_id=run_id,
        )
        connection.execute(
            """
            UPDATE engage_tiktok_runs
            SET status=?, evidence_ready=?, observed_account=?, updated_at=?
            WHERE run_id=?
            """,
            (
                status,
                evidence_ready,
                observed_account,
                "2026-08-22T00:00:00+00:00",
                run_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _command_value(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def test_collect_links_uses_mocked_browser_and_persists_only_safe_canonical_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    calls: list[dict[str, Any]] = []

    async def fake_discover(source: str, count: int, **kwargs: Any) -> dict[str, Any]:
        calls.append({"source": source, "count": count, **kwargs})
        return _discovery(count)

    monkeypatch.setattr(runner, "discover_top_content_links", fake_discover)
    result = asyncio.run(
        runner.collect_links(
            database=database,
            source_url=SOURCE_URL,
            requested_count=3,
            run_id="safe-links",
            social_browser_state=tmp_path / "Profile 7 runtime" / "state.json",
        )
    )

    assert result["status"] == "links_complete"
    assert result["canonical_urls"] == [_video_url(index) for index in range(1, 4)]
    assert calls[0]["exclude_video_ids"] == set()
    assert (
        calls[0]["social_browser_runtime_dir"]
        == (tmp_path / "Profile 7 runtime").resolve()
    )
    with TopContentLinkState(database) as state:
        rows = state.links("safe-links")
        assert [row["canonical_url"] for row in rows] == result["canonical_urls"]
        assert all("?" not in row["canonical_url"] for row in rows)
        assert all("#" not in row["canonical_url"] for row in rows)
        assert not ({"caption", "comments", "media"} & set(rows[0]))


def test_collect_for_listen_freezes_master_exclusions_and_passes_known_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    master = tmp_path / "global master.sqlite3"
    known = {_video_id(900), _video_id(901)}
    observed_exclusions: list[set[str]] = []

    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot(known, Path(path)),
    )

    async def fake_discover(source: str, count: int, **kwargs: Any) -> dict[str, Any]:
        observed_exclusions.append(set(kwargs["exclude_video_ids"]))
        return _discovery(count, excluded=len(known))

    monkeypatch.setattr(runner, "discover_top_content_links", fake_discover)
    result = asyncio.run(
        runner.collect_links(
            database=database,
            source_url=SOURCE_URL,
            requested_count=4,
            run_id="listen-links",
            for_listen=True,
            master_database=master,
            social_browser_state=tmp_path / "social_browser" / "state.json",
        )
    )

    assert result["status"] == "links_complete"
    assert observed_exclusions == [known]
    with TopContentLinkState(database) as state:
        status = state.status("listen-links")
    assert status["collection_policy"] == "new_only_against_tiktok_master"
    assert status["known_excluded"]["count"] == len(known)
    assert status["master_database"]["path"] == str(master.resolve())
    assert status["master_database"]["sha256"] == runner.canonical_sha256(sorted(known))


def test_collect_more_than_one_hundred_spans_rankings_without_a_runner_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"

    async def fake_discover(source: str, count: int, **kwargs: Any) -> dict[str, Any]:
        assert count == 105
        return _discovery(count)

    monkeypatch.setattr(runner, "discover_top_content_links", fake_discover)
    result = asyncio.run(
        runner.collect_links(
            database=database,
            source_url=SOURCE_URL,
            requested_count=105,
            run_id="one-oh-five",
            social_browser_state=tmp_path / "social_browser" / "state.json",
        )
    )

    assert result["status_summary"] == "links_complete: 105/105"
    with TopContentLinkState(database) as state:
        rows = state.links("one-oh-five")
    assert rows[99]["ranking_key"] == "video_views"
    assert rows[99]["ranking_position"] == 100
    assert rows[100]["ranking_key"] == "engagement"
    assert rows[100]["ranking_position"] == 1


def test_resume_links_can_extend_a_frozen_partial_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    attempts = [_discovery(2), _discovery(3)]

    async def fake_discover(source: str, count: int, **kwargs: Any) -> dict[str, Any]:
        assert count == 3
        return attempts.pop(0)

    monkeypatch.setattr(runner, "discover_top_content_links", fake_discover)
    first = asyncio.run(
        runner.collect_links(
            database=database,
            source_url=SOURCE_URL,
            requested_count=3,
            run_id="resumable-links",
            social_browser_state=tmp_path / "social_browser" / "state.json",
        )
    )
    assert first["status"] == "links_incomplete"
    assert first["link_count"] == 2

    resumed = asyncio.run(
        runner.resume_links(
            database=database,
            run_id="resumable-links",
            social_browser_state=tmp_path / "social_browser" / "state.json",
        )
    )
    assert resumed["status"] == "links_complete"
    assert resumed["canonical_urls"] == [_video_url(index) for index in range(1, 4)]


def test_child_commands_are_argument_lists_with_globals_before_subcommands(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child database with spaces" / "state.sqlite3"
    master = tmp_path / "master database with spaces.sqlite3"
    browser = tmp_path / "browser runtime with spaces" / "state.json"
    fresh = runner._fresh_child_command(
        project="top_content_project",
        canonical_url=_video_url(1),
        child_database=child,
        master_database=master,
        max_comments=25,
        social_browser_state=browser,
        browser_startup_timeout=45.5,
        expected_account="bound.account",
    )
    fresh_subcommand = fresh.index("music-audit")
    assert isinstance(fresh, list)
    assert fresh.index("--database") < fresh_subcommand
    assert fresh.index("--master-database") < fresh_subcommand
    assert fresh[fresh.index("--database") + 1] == str(child)
    assert fresh[fresh.index("--master-database") + 1] == str(master)
    assert fresh[fresh.index("--url") + 1] == _video_url(1)
    assert fresh[fresh.index("--posts") + 1] == "1"
    assert fresh[fresh.index("--collection-policy") + 1] == "new_only"

    resume = runner._resume_child_command(
        child_run_id="child-run-1",
        child_database=child,
        master_database=master,
        social_browser_state=browser,
        browser_startup_timeout=45.5,
    )
    resume_subcommand = resume.index("resume-collect")
    assert resume.index("--database") < resume_subcommand
    assert resume.index("--master-database") < resume_subcommand
    assert resume[resume.index("--run-id") + 1] == "child-run-1"


def test_execute_child_preserves_space_arguments_and_forces_shell_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    command = [
        "C:/Python 311/python.exe",
        "D:/Workspace With Spaces/engage_tiktok.py",
        "--database",
        str(tmp_path / "child db.sqlite3"),
        "music-audit",
    ]

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args, 0, stdout='{"status":"collection_complete"}', stderr=""
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    exit_code, payload = runner._execute_child(command)

    assert exit_code == 0
    assert payload == {"status": "collection_complete"}
    assert captured["args"] == command
    assert captured["shell"] is False
    assert captured["check"] is False
    assert captured["cwd"] == str(runner.ROOT)


def test_known_master_post_is_skipped_without_launching_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    master = tmp_path / "master.sqlite3"
    child = tmp_path / "child.sqlite3"
    _make_complete_parent(database)
    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot({_video_id(1)}, Path(path)),
    )
    monkeypatch.setattr(
        runner,
        "_execute_child",
        lambda command: pytest.fail("a known post must not launch LISTEN"),
    )

    result = runner.run_listen(
        database=database,
        run_id="parent-run",
        project="known_skip_project",
        child_database=child,
        master_database=master,
    )

    assert result["listen_status"] == "listen_complete"
    assert result["new_evidence_complete"] == 0
    assert result["skipped_known"] == 1
    assert result["jobs"][0]["status"] == "skipped_known"
    assert not child.exists()


def test_fresh_child_success_is_recovered_from_sqlite_and_reconciles_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "parent links.sqlite3"
    child = tmp_path / "child database" / "engage state.sqlite3"
    master = tmp_path / "global master.sqlite3"
    _make_complete_parent(database)
    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot(set(), Path(path)),
    )
    commands: list[list[str]] = []

    def complete_child(command: list[str]) -> tuple[int, dict[str, Any]]:
        commands.append(command)
        assert "music-audit" in command
        _write_child_run(
            Path(_command_value(command, "--database")),
            run_id="child-complete-1",
            project=_command_value(command, "--project"),
            canonical_url=_command_value(command, "--url"),
            master_database=Path(_command_value(command, "--master-database")),
            status="collection_complete",
            evidence_ready=1,
            observed_account="bound.account",
        )
        return 0, {"status": "collection_complete"}

    monkeypatch.setattr(runner, "_execute_child", complete_child)
    first = runner.run_listen(
        database=database,
        run_id="parent-run",
        project="fresh_child_project",
        child_database=child,
        master_database=master,
    )
    assert first["listen_status"] == "listen_complete"
    assert first["new_evidence_complete"] == 1
    assert len(commands) == 1

    monkeypatch.setattr(
        runner,
        "_execute_child",
        lambda command: pytest.fail("completed jobs must reconcile without dispatch"),
    )
    second = runner.run_listen(
        database=database,
        run_id="parent-run",
        project="fresh_child_project",
        child_database=child,
        master_database=master,
    )
    assert second["listen_status"] == "listen_complete"
    assert second["jobs"][0]["child_run_id"] == "child-complete-1"

    connection = sqlite3.connect(child)
    try:
        connection.execute(
            "UPDATE engage_tiktok_runs SET evidence_ready=0 WHERE run_id=?",
            ("child-complete-1",),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(
        runner.TopContentRunnerError,
        match="completed child does not contain one evidence-ready post",
    ):
        runner.run_listen(
            database=database,
            run_id="parent-run",
            project="fresh_child_project",
            child_database=child,
            master_database=master,
        )


def test_exit_two_child_is_durable_incomplete_then_resumes_existing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    child = tmp_path / "child.sqlite3"
    master = tmp_path / "master.sqlite3"
    _make_complete_parent(database)
    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot(set(), Path(path)),
    )
    commands: list[list[str]] = []

    def first_dispatch(command: list[str]) -> tuple[int, dict[str, Any]]:
        commands.append(command)
        _write_child_run(
            child,
            run_id="child-incomplete-1",
            project=_command_value(command, "--project"),
            canonical_url=_command_value(command, "--url"),
            master_database=master,
            status="collection_incomplete",
            evidence_ready=0,
        )
        return 2, {"status": "collection_incomplete"}

    monkeypatch.setattr(runner, "_execute_child", first_dispatch)
    first = runner.run_listen(
        database=database,
        run_id="parent-run",
        project="resume_project",
        child_database=child,
        master_database=master,
    )
    assert first["listen_status"] == "listen_incomplete"
    assert first["jobs"][0]["status"] == "incomplete"
    assert first["jobs"][0]["child_run_id"] == "child-incomplete-1"

    def resume_dispatch(command: list[str]) -> tuple[int, dict[str, Any]]:
        commands.append(command)
        assert "resume-collect" in command
        assert "music-audit" not in command
        assert _command_value(command, "--run-id") == "child-incomplete-1"
        connection = sqlite3.connect(child)
        try:
            connection.execute(
                """
                UPDATE engage_tiktok_runs
                SET status='collection_complete', evidence_ready=1
                WHERE run_id='child-incomplete-1'
                """
            )
            connection.commit()
        finally:
            connection.close()
        return 0, {"status": "collection_complete"}

    monkeypatch.setattr(runner, "_execute_child", resume_dispatch)
    second = runner.run_listen(
        database=database,
        run_id="parent-run",
        project="resume_project",
        child_database=child,
        master_database=master,
    )
    assert second["listen_status"] == "listen_complete"
    assert second["jobs"][0]["status"] == "complete"
    assert len(commands) == 2


def test_multiple_matching_child_rows_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    child = tmp_path / "child.sqlite3"
    master = tmp_path / "master.sqlite3"
    _make_complete_parent(database)
    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot(set(), Path(path)),
    )
    for index in (1, 2):
        _write_child_run(
            child,
            run_id=f"ambiguous-child-{index}",
            project="ambiguous_project",
            canonical_url=_video_url(1),
            master_database=master,
            status="collection_incomplete",
            evidence_ready=0,
        )

    with pytest.raises(
        runner.TopContentRunnerError,
        match="multiple child runs match one frozen project and URL",
    ):
        runner.run_listen(
            database=database,
            run_id="parent-run",
            project="ambiguous_project",
            child_database=child,
            master_database=master,
        )


def test_existing_child_must_match_frozen_collection_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    child = tmp_path / "child.sqlite3"
    master = tmp_path / "master.sqlite3"
    _make_complete_parent(database)
    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot(set(), Path(path)),
    )
    _write_child_run(
        child,
        run_id="wrong-settings-child",
        project="settings_project",
        canonical_url=_video_url(1),
        master_database=master,
        status="collection_incomplete",
        evidence_ready=0,
    )

    with pytest.raises(
        runner.TopContentRunnerError,
        match="child comment bound does not match the bridge",
    ):
        runner.run_listen(
            database=database,
            run_id="parent-run",
            project="settings_project",
            child_database=child,
            master_database=master,
            max_comments=10,
        )


def test_initialized_listen_settings_cannot_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    child = tmp_path / "child.sqlite3"
    master = tmp_path / "master.sqlite3"
    _make_complete_parent(database)
    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot({_video_id(1)}, Path(path)),
    )
    runner.run_listen(
        database=database,
        run_id="parent-run",
        project="frozen_settings_project",
        child_database=child,
        master_database=master,
        max_comments=10,
    )

    with pytest.raises(
        runner.TopContentRunnerError,
        match="frozen LISTEN setting changed: max_comments",
    ):
        runner.run_listen(
            database=database,
            run_id="parent-run",
            project="frozen_settings_project",
            child_database=child,
            master_database=master,
            max_comments=11,
        )


def test_first_child_global_block_leaves_later_jobs_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "links.sqlite3"
    child = tmp_path / "child.sqlite3"
    master = tmp_path / "master.sqlite3"
    _make_complete_parent(database, count=2)
    monkeypatch.setattr(
        runner,
        "_known_snapshot",
        lambda path: _known_snapshot(set(), Path(path)),
    )
    dispatches: list[list[str]] = []

    def blocked_dispatch(command: list[str]) -> tuple[int, dict[str, Any]]:
        dispatches.append(command)
        return 1, {"status": "browser_blocked"}

    monkeypatch.setattr(runner, "_execute_child", blocked_dispatch)
    result = runner.run_listen(
        database=database,
        run_id="parent-run",
        project="blocked_project",
        child_database=child,
        master_database=master,
    )

    assert len(dispatches) == 1
    assert result["listen_status"] == "listen_incomplete"
    assert result["jobs"][0]["status"] == "blocked"
    assert result["jobs"][1]["status"] == "pending"
    assert "child_run_not_recoverable_after_dispatch" in result["jobs"][0]["reason"]
