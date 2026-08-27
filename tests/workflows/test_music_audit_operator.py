import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import engage_tiktok as engage


ROOT = Path(__file__).resolve().parents[2]
OPERATOR_PATH = (
    ROOT
    / ".agents"
    / "skills"
    / "google-3.1-music-audit-instructions"
    / "scripts"
    / "music_audit_operator.py"
)
SPEC = importlib.util.spec_from_file_location("music_audit_operator", OPERATOR_PATH)
assert SPEC and SPEC.loader
operator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = operator
SPEC.loader.exec_module(operator)


def terminal_record(post_id: str) -> dict:
    record = {
        "id": post_id,
        "url": f"https://www.tiktok.com/@maker/video/{post_id}",
        "username": "maker",
        "creator_display_name": "Maker",
        "content_type": "video",
        "caption": "A complete music post",
        "view_count": 10,
        "like_count": 2,
        "comment_count": 0,
        "share_count": 0,
        "music_id": "music-1",
        "music_title": "Known Song",
        "music_author": "Known Artist",
        "music_duration_seconds": 180,
        "post_duration_seconds": 15,
        "music_metadata_status": "available",
        "comments": [],
        "ok": True,
        "complete": True,
        "exhausted": True,
        "limit_reached": False,
        "has_more": False,
        "source": "direct_api",
        "transcript_status": "unavailable",
    }
    platform_music = engage.platform_music_observation(record)
    musicbrainz = engage.terminal_musicbrainz_result(
        platform_music,
        status="unsupported",
        reason="test_terminal",
    )
    record["music_evidence"] = engage.build_music_evidence(
        record,
        configured_catalogs=("musicbrainz",),
        musicbrainz_result=musicbrainz,
    )
    return record


class ReadyPreflight:
    async def ensure_ready(self):
        return {
            "browser_endpoint_configured": True,
            "reachable": True,
            "tiktok_authenticated": True,
            "observed_account": "collector",
            "profile": {
                "verified": True,
                "profile_directory": "Profile 7",
                "mode": "existing_profile_attach",
                "verification_method": "test",
            },
        }


class ReplacementCollector:
    last_diagnostics = {"collection_stop_reason": "requested_count_reached"}

    async def collect(
        self,
        *,
        record_callback,
        existing_post_ids=(),
        initial_evidence_ready_count=0,
        music_catalogs=(),
        candidate_reserver=None,
        **_kwargs,
    ):
        assert tuple(existing_post_ids) == ()
        assert initial_evidence_ready_count == 0
        assert tuple(music_catalogs) == ("musicbrainz",)
        records = []
        for post_id in ("101", "102", "103"):
            record = terminal_record(post_id)
            record["topic_relevance_required"] = True
            record["topic_relevance"] = {"decision": "review"}
            assert candidate_reserver is not None
            assert candidate_reserver(post_id, record)
            record_callback(record)
            records.append(record)
        accepted = terminal_record("104")
        accepted["topic_relevance_required"] = True
        accepted["topic_relevance"] = {"decision": "accept"}
        assert candidate_reserver("104", accepted)
        record_callback(accepted)
        records.append(accepted)
        return records


class DirectCollector:
    last_diagnostics = {"collection_stop_reason": "requested_count_reached"}

    async def collect(
        self,
        *,
        record_callback,
        existing_post_ids=(),
        initial_evidence_ready_count=0,
        collection_policy="new_only",
        global_known_post_ids=(),
        current_run_post_ids=(),
        refresh_candidates=(),
        candidate_reserver=None,
        source_mode="topic",
        direct_post_url="",
        music_catalogs=(),
        **_kwargs,
    ):
        assert tuple(existing_post_ids) == ()
        assert initial_evidence_ready_count == 0
        assert collection_policy == "new_only"
        assert tuple(global_known_post_ids) == ()
        assert tuple(current_run_post_ids) == ()
        assert tuple(refresh_candidates) == ()
        assert source_mode == "url"
        assert direct_post_url == "https://www.tiktok.com/@maker/video/123"
        assert tuple(music_catalogs) == ("musicbrainz",)
        record = terminal_record("123")
        assert candidate_reserver is not None
        assert candidate_reserver("123", record)
        assert record_callback(record) is True
        return [record]


class CreatorCollector:
    last_diagnostics = {"collection_stop_reason": "requested_count_reached"}

    async def collect(
        self,
        *,
        record_callback,
        existing_post_ids=(),
        initial_evidence_ready_count=0,
        collection_policy="new_only",
        global_known_post_ids=(),
        current_run_post_ids=(),
        refresh_candidates=(),
        candidate_reserver=None,
        source_mode="topic",
        creator_handle="",
        creator_inventory=(),
        creator_inventory_terminal=False,
        creator_selected_post_ids=(),
        creator_inventory_callback=None,
        music_catalogs=(),
        **_kwargs,
    ):
        assert tuple(existing_post_ids) == ()
        assert initial_evidence_ready_count == 0
        assert collection_policy == "new_only"
        assert tuple(global_known_post_ids) == ()
        assert tuple(current_run_post_ids) == ()
        assert tuple(refresh_candidates) == ()
        assert source_mode == "creator"
        assert creator_handle == "maker"
        assert tuple(creator_inventory) == ()
        assert creator_inventory_terminal is False
        assert tuple(creator_selected_post_ids) == ()
        assert tuple(music_catalogs) == ("musicbrainz",)
        record = terminal_record("201")
        assert creator_inventory_callback is not None
        assert (
            creator_inventory_callback(
                [record],
                {
                    "terminal": True,
                    "terminal_verified": True,
                    "inventory_complete": True,
                    "selected_post_ids": ["201"],
                    "creator_identity": {"handle": "maker"},
                    "observed_at": "2026-08-23T00:00:00+07:00",
                },
            )
            == 1
        )
        assert candidate_reserver is not None
        assert candidate_reserver("201", record)
        assert record_callback(record) is True
        return [record]


@pytest.fixture
def completed_operator_run(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=4,
        resolved_max_pages=4,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="operator_test",
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
    )
    database = Path(handoff["database"])
    conn = engage.connect_database(
        database,
        master_database=paths.master_database,
        sync_master=True,
    )
    try:
        run_id = engage.create_run(
            conn,
            project="operator_test",
            topic="music",
            requested_count=1,
            max_comments=20,
            max_pages=4,
            mode="shadow",
            workflow="listen",
            collection_policy="new_only",
            source_mode="topic",
            master_database=str(paths.master_database),
        )
        status = asyncio.run(
            engage.collect_exact(
                conn,
                run_id=run_id,
                preflight=ReadyPreflight(),
                collector=ReplacementCollector(),
            )
        )
        assert status["status"] == "collection_complete"
        assert status["evidence_ready"] == 1
        assert status["unique_collected"] == 4
        assert status["failed"] == 3
        assert (
            engage.export_listen_evidence(
                conn,
                run_id,
                Path(handoff["export_file"]),
            )
            == 1
        )
    finally:
        conn.close()
    handoff["run_id"] = run_id
    operator.capture_frozen_selection(handoff)
    handoff["state"] = "collection_complete"
    handoff["last_status"] = operator.status_summary(status)
    operator.write_handoff(handoff_path, handoff)
    operator.append_ledger(
        handoff,
        action="fixture-collection-complete",
        stage="collection",
        paths=paths,
        status=status,
        next_action="validate",
    )
    return paths, handoff_path, handoff


@pytest.fixture
def blocked_operator_run(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=4,
        resolved_max_pages=4,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="operator_blocked_test",
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
    )
    conn = engage.connect_database(
        Path(handoff["database"]),
        master_database=paths.master_database,
        sync_master=True,
    )
    try:
        run_id = engage.create_run(
            conn,
            project="operator_blocked_test",
            topic="music",
            requested_count=1,
            max_comments=20,
            max_pages=4,
            mode="shadow",
            workflow="listen",
            collection_policy="new_only",
            source_mode="topic",
            master_database=str(paths.master_database),
        )
        conn.execute(
            """
            UPDATE engage_tiktok_runs
            SET status='browser_blocked', error='Profile 7 unavailable'
            WHERE run_id=?
            """,
            (run_id,),
        )
        conn.commit()
        engage._register_master_run_state(conn, run_id)
        status = engage.run_status(conn, run_id)
    finally:
        conn.close()
    handoff["run_id"] = run_id
    operator.capture_frozen_selection(handoff)
    handoff["state"] = "browser_blocked"
    handoff["last_status"] = operator.status_summary(status)
    operator.write_handoff(handoff_path, handoff)
    operator.append_ledger(
        handoff,
        action="music-audit-start",
        stage="collection",
        paths=paths,
        command=(
            str(paths.python),
            str(paths.engage_script),
            "music-audit",
        ),
        next_action="capture_run_id",
    )
    operator.append_ledger(
        handoff,
        action="music-audit-result",
        stage="collection",
        paths=paths,
        exit_code=1,
        status=status,
        error="native_edge_crash 0xc0000005",
        next_action="bounded_profile7_recovery",
    )
    return paths, handoff_path, handoff


@pytest.fixture
def completed_direct_operator_run(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=1,
        resolved_max_pages=1,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    direct_url = "https://www.tiktok.com/@maker/video/123"
    handoff_path, handoff = operator.new_handoff(
        project="operator_direct_test",
        source_mode="url",
        source_target=direct_url,
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
    )
    conn = engage.connect_database(
        Path(handoff["database"]),
        master_database=paths.master_database,
        sync_master=True,
    )
    try:
        run_id = engage.create_run(
            conn,
            project="operator_direct_test",
            topic="",
            requested_count=1,
            max_comments=20,
            max_pages=1,
            mode="shadow",
            workflow="listen",
            collection_policy="new_only",
            source_mode="url",
            direct_post_url=direct_url,
            master_database=str(paths.master_database),
        )
        status = asyncio.run(
            engage.collect_exact(
                conn,
                run_id=run_id,
                preflight=ReadyPreflight(),
                collector=DirectCollector(),
            )
        )
        assert status["status"] == "collection_complete"
        assert (
            engage.export_listen_evidence(
                conn,
                run_id,
                Path(handoff["export_file"]),
            )
            == 1
        )
    finally:
        conn.close()
    handoff["run_id"] = run_id
    operator.capture_frozen_selection(handoff)
    handoff["state"] = "collection_complete"
    handoff["last_status"] = operator.status_summary(status)
    operator.write_handoff(handoff_path, handoff)
    operator.append_ledger(
        handoff,
        action="fixture-direct-collection-complete",
        stage="collection",
        paths=paths,
        status=status,
        next_action="validate",
    )
    return paths, handoff_path, handoff


@pytest.fixture
def completed_creator_operator_run(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=0,
        resolved_max_pages=0,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="operator_creator_test",
        source_mode="creator",
        source_target="@maker",
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
    )
    conn = engage.connect_database(
        Path(handoff["database"]),
        master_database=paths.master_database,
        sync_master=True,
    )
    try:
        run_id = engage.create_run(
            conn,
            project="operator_creator_test",
            topic="creator:@maker",
            requested_count=1,
            max_comments=20,
            max_pages=0,
            mode="shadow",
            workflow="listen",
            collection_policy="new_only",
            source_mode="creator",
            creator_handle="maker",
            master_database=str(paths.master_database),
        )
        status = asyncio.run(
            engage.collect_exact(
                conn,
                run_id=run_id,
                preflight=ReadyPreflight(),
                collector=CreatorCollector(),
            )
        )
        assert status["status"] == "collection_complete"
        assert (
            engage.export_listen_evidence(
                conn,
                run_id,
                Path(handoff["export_file"]),
            )
            == 1
        )
    finally:
        conn.close()
    handoff["run_id"] = run_id
    operator.capture_frozen_selection(handoff)
    handoff["state"] = "collection_complete"
    handoff["last_status"] = operator.status_summary(status)
    operator.write_handoff(handoff_path, handoff)
    operator.append_ledger(
        handoff,
        action="fixture-creator-collection-complete",
        stage="collection",
        paths=paths,
        status=status,
        next_action="validate",
    )
    return paths, handoff_path, handoff


@pytest.fixture
def refresh_selection_run(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    cutoff = "2026-08-22T00:00:00+07:00"
    args = SimpleNamespace(
        collection_policy="refresh_known",
        max_comments=20,
        max_pages=4,
        resolved_max_pages=4,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before=cutoff,
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="operator_refresh_binding_test",
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
    )
    candidates = [
        {
            "id": "301",
            "url": "https://www.tiktok.com/@maker/video/301",
            "username": "maker",
            "content_type": "video",
        },
        {
            "id": "302",
            "url": "https://www.tiktok.com/@maker/video/302",
            "username": "maker",
            "content_type": "video",
        },
    ]
    conn = engage.connect_database(
        Path(handoff["database"]),
        master_database=paths.master_database,
        sync_master=True,
    )
    try:
        run_id = engage.create_run(
            conn,
            project="operator_refresh_binding_test",
            topic="music",
            requested_count=1,
            max_comments=20,
            max_pages=4,
            mode="shadow",
            workflow="listen",
            collection_policy="refresh_known",
            source_mode="topic",
            refresh_candidates=candidates,
            refresh_stale_before=cutoff,
            master_database=str(paths.master_database),
        )
    finally:
        conn.close()
    handoff["run_id"] = run_id
    operator.capture_frozen_selection(handoff)
    operator.write_handoff(handoff_path, handoff)
    return paths, handoff_path, handoff


def test_completed_validation_accepts_replacement_candidate_history(
    completed_operator_run,
):
    paths, _handoff_path, handoff = completed_operator_run
    review = operator.validate_completed_artifacts(handoff, paths=paths)

    assert review["executor_compliance"] == "PASS"
    assert review["task_outcome"] == "COMPLETE"
    assert review["status"]["requested"] == 1
    assert review["status"]["evidence_ready"] == 1
    assert review["status"]["unique_collected"] == 4
    assert review["status"]["failed"] == 3
    assert review["artifacts"]["evidence_export"]["records"] == 1
    assert review["output_layout"] == {
        "schema_version": "tiktok-music-audit-project-layout-v2",
        "artifact_stem": handoff["artifact_stem"],
        "project_directory": handoff["project_root"],
        "workflow_database": handoff["database"],
        "handoff": handoff["handoff_file"],
        "operator_ledger": handoff["ledger_file"],
        "evidence_export": handoff["export_file"],
        "evidence_export_status": "validated",
        "machine_result": handoff["review_json"],
        "review_log": handoff["review_markdown"],
    }
    assert "## Project output" in operator.review_markdown(review)
    assert review["ai_actions"] == []
    assert review["outbound_actions"] == []


def test_legacy_project_layout_remains_fixed_for_existing_handoffs(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )

    layout = operator.canonical_project_layout(
        paths,
        "music_audit_layout_test",
    )

    assert layout.project_root == (
        tmp_path / "comments_data" / "project_music_audit_layout_test"
    ).resolve()
    assert layout.database == layout.project_root / "state" / "engage_state.sqlite"
    assert layout.handoff_file == (
        layout.project_root / "music_audit_operator_handoff.json"
    )
    assert layout.ledger_file == (
        layout.project_root / "music_audit_operator_ledger.jsonl"
    )
    assert layout.export_file == layout.project_root / "music_audit_evidence.jsonl"
    assert layout.review_json == layout.project_root / "music_audit_review.json"
    assert layout.review_markdown == (
        layout.project_root / "log_file_music_audit_review.md"
    )


def test_semantic_project_layout_uses_one_run_specific_stem(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    stem = "test_music_1p_260825_1234abcd"
    layout = operator.canonical_project_layout(
        paths,
        "music_audit_test_topic_music_1p_20260825",
        schema_version=operator.PROJECT_LAYOUT_SCHEMA,
        artifact_stem=stem,
    )

    assert layout.schema_version == "tiktok-music-audit-project-layout-v2"
    assert layout.artifact_stem == stem
    assert layout.database == layout.project_root / "state" / f"{stem}_state.sqlite"
    assert layout.handoff_file == layout.project_root / f"{stem}_handoff.json"
    assert layout.ledger_file == layout.project_root / f"{stem}_ledger.jsonl"
    assert layout.export_file == layout.project_root / f"{stem}_evidence.jsonl"
    assert layout.review_json == layout.project_root / f"{stem}_review.json"
    assert layout.review_markdown == layout.project_root / f"{stem}_review.md"


def test_automatic_test_topic_naming_is_semantic_and_deterministic():
    naming = operator.automatic_run_naming(
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        collection_policy="new_only",
        run_label_input="Test",
        timestamp="20260825_191612_984716",
    )

    assert naming.project == (
        "music_audit_test_new_topic_music_1p_20260825_191612_984716"
    )
    assert naming.run_label == "test"
    assert naming.run_label_input == "Test"
    assert naming.run_descriptor == "test_new_topic_music_1p"
    assert naming.artifact_stem.startswith("test_1p_music_260825_")
    assert len(naming.artifact_stem) <= operator.ARTIFACT_STEM_MAX


def test_automatic_creator_all_and_direct_url_names_bind_real_scope():
    creator = operator.automatic_run_naming(
        source_mode="creator",
        source_target="https://www.tiktok.com/@scarlettofficial",
        requested_count=0,
        all_posts=True,
        collection_policy="new_only",
        timestamp="20260825_191612_111111",
    )
    direct = operator.automatic_run_naming(
        source_mode="url",
        source_target="https://www.tiktok.com/@maker/video/7654838350913867028",
        requested_count=1,
        all_posts=False,
        collection_policy="new_only",
        timestamp="20260825_191612_222222",
    )

    assert creator.project == (
        "music_audit_new_creator_scarlettofficial_all_20260825_191612_111111"
    )
    assert creator.artifact_stem.startswith("creator_all_scarlettof")
    assert "7654838350913867028_1p" in direct.project
    assert direct.artifact_stem.startswith("url_1p_765483835091386")


def test_brand_label_and_refresh_are_explicit_naming_metadata_only():
    naming = operator.automatic_run_naming(
        source_mode="creator",
        source_target="@miesedaap",
        requested_count=5,
        all_posts=False,
        collection_policy="refresh_known",
        run_label_input="Brand: Mie Sedaap",
        timestamp="20260825_191612_333333",
    )

    assert naming.run_label == "brand_mie_sedaap"
    assert naming.run_descriptor == (
        "brand_mie_sedaap_refresh_creator_miesedaap_5p"
    )
    assert naming.project.startswith("music_audit_brand_mie_sedaap_")
    assert naming.project.endswith("_20260825_191612_333333")
    assert len(naming.project) <= operator.NEW_PROJECT_SLUG_MAX
    assert naming.artifact_stem.startswith("refresh_brand_mie_sed")


def test_long_semantic_names_are_hash_distinct_and_path_bounded():
    common = "brand-" + ("campaign-" * 10)
    first = operator.automatic_run_naming(
        source_mode="topic",
        source_target="Indonesian pop music and emerging artists",
        requested_count=50,
        all_posts=False,
        collection_policy="new_only",
        run_label_input=common + "one",
        timestamp="20260825_191612_444444",
    )
    second = operator.automatic_run_naming(
        source_mode="topic",
        source_target="Indonesian pop music and emerging artists",
        requested_count=50,
        all_posts=False,
        collection_policy="new_only",
        run_label_input=common + "two",
        timestamp="20260825_191612_444444",
    )

    assert first.project != second.project
    assert first.artifact_stem != second.artifact_stem
    assert len(first.project) <= operator.NEW_PROJECT_SLUG_MAX
    assert len(first.artifact_stem) <= operator.ARTIFACT_STEM_MAX
    layout = operator.canonical_project_layout(
        operator.canonical_paths(),
        first.project,
        schema_version=operator.PROJECT_LAYOUT_SCHEMA,
        artifact_stem=first.artifact_stem,
    )
    export_temp = layout.export_file.with_name(
        f".{layout.export_file.name}.{'0' * 32}.tmp"
    )
    assert len(str(export_temp)) <= operator.WINDOWS_LEGACY_PATH_MAX


@pytest.mark.parametrize("label", ["..", "folder/name", "folder\\name", "bad\nname"])
def test_run_label_rejects_path_or_control_input(label):
    with pytest.raises(operator.OperatorError):
        operator.automatic_run_naming(
            source_mode="topic",
            source_target="music",
            requested_count=1,
            all_posts=False,
            collection_policy="new_only",
            run_label_input=label,
            timestamp="20260825_191612_555555",
        )


def test_start_rejects_project_and_run_label_together():
    args = operator.build_parser().parse_args(
        [
            "start",
            "--topic",
            "music",
            "--posts",
            "1",
            "--project",
            "music_audit_manual",
            "--run-label",
            "test",
        ]
    )

    with pytest.raises(operator.OperatorError, match="either --project or --run-label"):
        operator.validate_start_scope(args)


@pytest.mark.parametrize(
    "project",
    [
        "brand_music",
        "project_music_audit_brand",
        "music_audit_",
        "Music_Audit_Brand",
        "music_audit_brand.",
    ],
)
def test_guarded_start_rejects_noncanonical_project_slugs(project):
    with pytest.raises(operator.OperatorError):
        operator._safe_project_slug(project)


def test_guarded_start_accepts_canonical_project_slug():
    assert (
        operator._safe_project_slug("music_audit_brand_20260825")
        == "music_audit_brand_20260825"
    )


def test_guarded_start_rejects_overlong_new_project_slug():
    project = "music_audit_" + ("x" * operator.NEW_PROJECT_SLUG_MAX)

    with pytest.raises(operator.OperatorError, match="at most"):
        operator._safe_project_slug(project)


def test_handoff_rejects_rehashed_artifact_path_drift(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=4,
        resolved_max_pages=4,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="music_audit_path_drift",
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
    )
    handoff["export_file"] = str(tmp_path / "results.json")
    handoff["handoff_hash"] = operator._handoff_hash(handoff)

    with pytest.raises(operator.OperatorError, match="canonical path"):
        operator.validate_handoff(handoff, path=handoff_path, paths=paths)


def test_existing_layout_v1_handoff_without_layout_marker_still_loads(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=4,
        resolved_max_pages=4,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="music_audit_legacy_layout",
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
        project_layout_schema=operator.LEGACY_PROJECT_LAYOUT_SCHEMA,
    )
    legacy_only_fields = (
        "project_layout_schema",
        "artifact_stem",
        "run_label",
        "run_label_input",
        "run_descriptor",
    )
    for field in legacy_only_fields:
        handoff.pop(field, None)
        handoff["intent"].pop(field, None)
    handoff["intent_hash"] = operator.json_hash(handoff["intent"])
    operator.write_handoff(handoff_path, handoff)

    loaded = operator.load_handoff(handoff_path, paths)

    assert "project_layout_schema" not in loaded
    assert Path(loaded["database"]).name == "engage_state.sqlite"
    assert Path(loaded["handoff_file"]).name == "music_audit_operator_handoff.json"
    assert operator.output_layout_summary(
        loaded,
        evidence_export_status="not_created",
    )["schema_version"] == operator.LEGACY_PROJECT_LAYOUT_SCHEMA


def test_bound_handoff_rejects_missing_canonical_database(tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=4,
        resolved_max_pages=4,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="music_audit_missing_database",
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        args=args,
        paths=paths,
    )
    handoff["run_id"] = "engage_0123456789abcdef"
    handoff["handoff_hash"] = operator._handoff_hash(handoff)

    with pytest.raises(operator.OperatorError, match="database is missing"):
        operator.validate_handoff(handoff, path=handoff_path, paths=paths)


def test_completed_direct_url_validation_accepts_semantic_scope_urls(
    completed_direct_operator_run,
):
    paths, _handoff_path, handoff = completed_direct_operator_run

    review = operator.validate_completed_artifacts(handoff, paths=paths)

    assert review["task_outcome"] == "COMPLETE"
    assert review["source_mode"] == "url"
    assert review["posts"][0]["post_id"] == "123"
    assert review["posts"][0]["url"] == handoff["source_target"]


def test_completed_creator_validation_accepts_frozen_terminal_inventory(
    completed_creator_operator_run,
):
    paths, _handoff_path, handoff = completed_creator_operator_run

    review = operator.validate_completed_artifacts(handoff, paths=paths)

    assert review["task_outcome"] == "COMPLETE"
    assert review["source_mode"] == "creator"
    assert review["posts"][0]["creator"] == "maker"
    assert review["creator_inventory"]["terminal_verified"] is True
    assert review["creator_inventory"]["inventory_count"] == 1
    assert review["creator_inventory"]["selected_new_count"] == 1
    assert review["creator_inventory"]["new_only_excluded_count"] == 0
    assert "Creator inventory coverage" in operator.review_markdown(review)


def test_automatic_refresh_selection_is_frozen_and_intent_bound(
    refresh_selection_run,
):
    paths, handoff_path, handoff = refresh_selection_run

    run = operator.validate_run_intent(handoff, paths=paths)
    loaded = operator.load_handoff(handoff_path, paths)

    assert run["collection_policy"] == "refresh_known"
    assert loaded["frozen_selection"] == {
        "collection_policy": "refresh_known",
        "refresh_post_ids": ["301", "302"],
        "refresh_candidate_post_ids": ["301", "302"],
        "refresh_candidate_count": 2,
        "refresh_candidates_hash": loaded["frozen_selection"][
            "refresh_candidates_hash"
        ],
        "refresh_stale_before": "2026-08-22T00:00:00+07:00",
    }
    assert operator.SHA256_PATTERN.fullmatch(
        loaded["frozen_selection"]["refresh_candidates_hash"]
    )
    assert loaded["frozen_selection_hash"] == operator.json_hash(
        loaded["frozen_selection"]
    )


@pytest.mark.parametrize("mutation", ["empty", "reorder", "alter"])
def test_refresh_selection_drift_is_rejected(refresh_selection_run, mutation):
    paths, _handoff_path, handoff = refresh_selection_run
    database = Path(handoff["database"])
    conn = engage.sqlite3.connect(database)
    try:
        row = conn.execute(
            "SELECT refresh_candidates_json FROM engage_tiktok_runs WHERE run_id=?",
            (handoff["run_id"],),
        ).fetchone()
        candidates = json.loads(row[0])
        if mutation == "empty":
            candidates = []
        elif mutation == "reorder":
            candidates.reverse()
        else:
            candidates[0]["url"] = "https://www.tiktok.com/@maker/video/999"
        conn.execute(
            "UPDATE engage_tiktok_runs SET refresh_candidates_json=? WHERE run_id=?",
            (engage.canonical_json(candidates), handoff["run_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        operator.OperatorError,
        match="frozen refresh selection drifted",
    ):
        operator.validate_run_intent(handoff, paths=paths)


def test_validation_rejects_recomputed_but_noncanonical_projection(
    completed_operator_run,
):
    paths, _handoff_path, handoff = completed_operator_run
    export_path = Path(handoff["export_file"])
    row = json.loads(export_path.read_text(encoding="utf-8"))
    row["evidence_packet"]["caption"] = "tampered projection"
    row["evidence_projection_hash"] = operator.json_hash(row["evidence_packet"])
    export_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(
        operator.OperatorError,
        match="does not match canonical raw evidence",
    ):
        operator.validate_completed_artifacts(handoff, paths=paths)


def test_validation_rejects_master_snapshot_hash_drift(completed_operator_run):
    paths, _handoff_path, handoff = completed_operator_run
    conn = engage.sqlite3.connect(paths.master_database)
    try:
        conn.execute(
            "UPDATE tiktok_master_snapshots SET evidence_hash=? WHERE local_run_id=?",
            ("0" * 64, handoff["run_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(operator.OperatorError, match="master snapshot evidence hash"):
        operator.validate_completed_artifacts(handoff, paths=paths)


def test_validation_rejects_authorization_artifacts(completed_operator_run):
    paths, _handoff_path, handoff = completed_operator_run
    conn = engage.sqlite3.connect(Path(handoff["database"]))
    try:
        conn.execute(
            """
            UPDATE engage_tiktok_posts
            SET authorization_target_url='https://www.tiktok.com/@maker/video/104'
            WHERE run_id=? AND evidence_ready=1
            """,
            (handoff["run_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(operator.OperatorError, match="authorization_target_url"):
        operator.validate_completed_artifacts(handoff, paths=paths)


def test_validation_rejects_master_source_identity_drift(completed_operator_run):
    paths, _handoff_path, handoff = completed_operator_run
    conn = engage.sqlite3.connect(paths.master_database)
    try:
        conn.execute(
            "UPDATE tiktok_master_runs SET topic='different' WHERE local_run_id=?",
            (handoff["run_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        operator.OperatorError, match="local/master run mismatch: topic"
    ):
        operator.validate_completed_artifacts(handoff, paths=paths)


def test_validation_rejects_master_comment_attempt(completed_operator_run):
    paths, _handoff_path, handoff = completed_operator_run
    conn = engage.sqlite3.connect(paths.master_database)
    conn.row_factory = engage.sqlite3.Row
    try:
        master_run = conn.execute(
            "SELECT source_id FROM tiktok_master_runs WHERE local_run_id=?",
            (handoff["run_id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO tiktok_master_comment_attempts (
                attempt_id, account_key, post_id, publication_id, source_id,
                local_run_id, target_url, text_hash, state,
                local_attempt_number, reserved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "attempt-test",
                "collector",
                "104",
                "publication-test",
                master_run["source_id"],
                handoff["run_id"],
                "https://www.tiktok.com/@maker/video/104",
                "1" * 64,
                "reserved",
                1,
                "2026-08-23T00:00:00+07:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(operator.OperatorError, match="prohibited comment attempt"):
        operator.validate_completed_artifacts(handoff, paths=paths)


@pytest.mark.parametrize(
    "unsafe",
    [
        "social_browser.py",
        "--headless=new",
        "--remote-debugging-port=9222",
        "--user-data-dir=C:/temp",
        "taskkill",
    ],
)
def test_operator_rejects_unsafe_command_fragments(tmp_path, unsafe):
    paths = operator.OperatorPaths(
        workspace=tmp_path,
        python=operator.REQUIRED_PYTHON,
        engage_script=tmp_path / "engage_tiktok.py",
        master_database=tmp_path / "master.sqlite",
    )
    command = [
        str(paths.python),
        str(paths.engage_script),
        "--database",
        str(tmp_path / "local.sqlite"),
        "music-audit",
        unsafe,
    ]
    with pytest.raises(operator.OperatorError, match="forbidden argument"):
        operator.assert_safe_command(command, paths)


def test_run_engage_uses_argv_without_shell(monkeypatch, tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path,
        python=operator.REQUIRED_PYTHON,
        engage_script=tmp_path / "engage_tiktok.py",
        master_database=tmp_path / "master.sqlite",
    )
    observed = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            self.returncode = 0
            self.pid = 123

        def communicate(self, timeout=None):
            observed["timeout"] = timeout
            return '{"status":"ok"}', ""

    monkeypatch.setattr(operator.subprocess, "Popen", FakePopen)
    result = operator.run_engage(
        [
            "--database",
            str(tmp_path / "local.sqlite"),
            "status",
            "--run-id",
            "engage_0123456789abcdef",
        ],
        paths=paths,
    )

    assert result.exit_code == 0
    assert isinstance(observed["argv"], tuple)
    assert observed["argv"][0] == str(operator.REQUIRED_PYTHON)
    assert observed["kwargs"]["stdout"] is operator.subprocess.PIPE
    assert observed["kwargs"]["stderr"] is operator.subprocess.PIPE
    assert "shell" not in observed["kwargs"]


def test_run_engage_launch_failure_does_not_claim_a_child(monkeypatch, tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path,
        python=operator.REQUIRED_PYTHON,
        engage_script=tmp_path / "engage_tiktok.py",
        master_database=tmp_path / "master.sqlite",
    )
    claimed = []

    def fail_launch(*_args, **_kwargs):
        raise OSError("cannot launch")

    monkeypatch.setattr(operator.subprocess, "Popen", fail_launch)
    result = operator.run_engage(
        [
            "--database",
            str(tmp_path / "local.sqlite"),
            "status",
            "--run-id",
            "engage_0123456789abcdef",
        ],
        paths=paths,
        on_process_started=lambda: claimed.append(True),
    )

    assert result.child_started is False
    assert result.exit_code == -1
    assert claimed == []
    assert result.payload["error"] == "collector_launch_failed"


def test_registration_failure_reaps_the_exact_spawned_child(
    monkeypatch,
    blocked_operator_run,
):
    paths, _handoff_path, handoff = blocked_operator_run
    operator.begin_execution(handoff, operation="resume")
    observed = {"terminated": False, "reaped": False}

    class FakePopen:
        def __init__(self, _argv, **_kwargs):
            self.pid = 778
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            observed["terminated"] = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def communicate(self, timeout=None):
            observed["reaped"] = True
            return "", ""

    monkeypatch.setattr(operator.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        operator,
        "process_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            operator.OperatorError("identity unavailable")
        ),
    )

    result = operator.run_engage(
        operator.build_resume_arguments(handoff),
        paths=paths,
        handoff=handoff,
    )

    assert result.child_started is False
    assert observed == {"terminated": True, "reaped": True}


def test_heartbeat_write_failure_does_not_abandon_collector(
    monkeypatch,
    blocked_operator_run,
):
    paths, _handoff_path, handoff = blocked_operator_run
    operator.begin_execution(handoff, operation="resume")
    observed = {"communicate": 0, "terminated": False, "writes": 0}

    class FakePopen:
        def __init__(self, _argv, **_kwargs):
            self.pid = 779
            self.returncode = 0

        def communicate(self, timeout=None):
            observed["communicate"] += 1
            if observed["communicate"] == 1:
                raise operator.subprocess.TimeoutExpired("collector", timeout)
            return '{"status":"browser_blocked"}', ""

        def terminate(self):
            observed["terminated"] = True

    original_write = operator.write_handoff

    def flaky_write(path, value):
        observed["writes"] += 1
        if observed["writes"] == 2:
            raise OSError("transient heartbeat write failure")
        return original_write(path, value)

    monkeypatch.setattr(operator.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        operator,
        "process_identity",
        lambda pid, *, role: {
            "role": role,
            "pid": pid,
            "create_time": 1.0,
            "executable": str(paths.python),
        },
    )
    monkeypatch.setattr(operator, "recorded_process_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(operator, "write_handoff", flaky_write)

    result = operator.run_engage(
        operator.build_resume_arguments(handoff),
        paths=paths,
        handoff=handoff,
        heartbeat_interval=0.01,
    )

    assert result.exit_code == 0
    assert observed["communicate"] == 2
    assert observed["terminated"] is False


def test_error_sanitizer_redacts_bearer_tokens_and_signed_urls():
    sanitized = operator.sanitize_error(
        "failed https://media.example/audio.mp3?X-Amz-Signature=SIGNED "
        "Authorization: Bearer SUPERSECRET"
    )

    assert "SUPERSECRET" not in sanitized
    assert "SIGNED" not in sanitized
    assert "media.example" not in sanitized
    assert "<redacted-url>" in sanitized


def test_project_slug_cannot_be_used_as_run_id(completed_operator_run):
    paths, handoff_path, handoff = completed_operator_run
    handoff["run_id"] = handoff["project"]
    operator.write_handoff(handoff_path, handoff)

    with pytest.raises(operator.OperatorError, match="run_id"):
        operator.load_handoff(handoff_path, paths)


def test_guarded_creator_all_rejects_a_finite_page_bound():
    args = operator.build_parser().parse_args(
        [
            "start",
            "--creator",
            "@maker",
            "--all-posts",
            "--max-pages",
            "10",
        ]
    )

    with pytest.raises(operator.OperatorError, match="cannot be combined"):
        operator.validate_start_scope(args)


def test_guarded_creator_all_is_uncapped_and_omits_max_pages():
    args = operator.build_parser().parse_args(
        ["start", "--creator", "@maker", "--all-posts"]
    )

    source_mode, source_target, requested, all_posts = operator.validate_start_scope(
        args
    )
    operator.freeze_start_options(
        args,
        source_mode=source_mode,
        source_target=source_target,
        requested_count=requested,
    )
    handoff = {
        "intent": {"max_pages": 0},
        "database": "creator.sqlite",
        "master_database": "master.sqlite",
        "project": "creator_all",
        "source_mode": source_mode,
        "source_target": source_target,
        "all_posts": all_posts,
        "requested_count": requested,
        "max_comments": args.max_comments,
        "collection_policy": args.collection_policy,
        "browser_startup_timeout": args.browser_startup_timeout,
        "expected_account": "",
    }
    arguments = operator.build_start_arguments(handoff)

    assert args.resolved_max_pages == 0
    assert "--all-posts" in arguments
    assert "--posts" not in arguments
    assert "--max-pages" not in arguments


def test_legacy_capped_creator_all_is_reported_as_noncompliant():
    compliance, deviations = operator.executor_compliance_summary(
        {
            "intent": {"all_posts": True, "max_pages": 10},
            "execution_history": [],
        },
        [],
    )

    assert compliance == "FAIL"
    assert any("finite page bound" in value for value in deviations)


def test_creator_all_coverage_requires_and_reports_terminal_diagnostics():
    conn = engage.sqlite3.connect(":memory:")
    conn.row_factory = engage.sqlite3.Row
    conn.execute(
        """
        CREATE TABLE engage_tiktok_events (
            event_id INTEGER PRIMARY KEY,
            run_id TEXT,
            stage TEXT,
            event TEXT,
            payload_json TEXT
        )
        """
    )
    diagnostics = {
        "terminal_verified": True,
        "inventory_complete": True,
        "has_more": False,
        "stop_reason": "source_exhausted",
        "source_exhausted": True,
        "limit_reached": False,
        "unique_owner_posts_observed": 5,
    }
    conn.execute(
        """
        INSERT INTO engage_tiktok_events
            (event_id, run_id, stage, event, payload_json)
        VALUES (1, 'creator-all', 'collection', 'complete', ?)
        """,
        (json.dumps({"diagnostics": diagnostics}),),
    )
    run = {
        "run_id": "creator-all",
        "source_mode": "creator",
        "collection_policy": "new_only",
        "cardinality_mode": "all",
        "creator_inventory_json": json.dumps(
            [{"id": str(100 + value)} for value in range(5)]
        ),
        "creator_selected_post_ids_json": "[]",
        "profile_inventory_terminal": 1,
        "profile_inventory_count": 5,
        "max_pages": 0,
    }

    coverage = operator._creator_inventory_coverage(conn, run)
    conn.close()

    assert coverage == {
        "cardinality_mode": "all",
        "terminal_verified": True,
        "inventory_complete": True,
        "has_more": False,
        "frontier_stop_reason": "source_exhausted",
        "source_exhausted": True,
        "limit_reached": False,
        "unique_owner_posts_observed": 5,
        "inventory_count": 5,
        "selected_new_count": 0,
        "new_only_excluded_count": 5,
        "page_bound": 0,
        "uncapped": True,
    }


def test_direct_url_refresh_freezes_the_exact_post_id():
    args = SimpleNamespace(
        collection_policy="refresh_known",
        refresh_post_id=[],
        refresh_stale_before="",
        max_pages=0,
    )

    operator.freeze_start_options(
        args,
        source_mode="url",
        source_target="https://www.tiktok.com/@maker/video/123",
        requested_count=1,
    )

    assert args.refresh_post_id == ["123"]
    assert args.refresh_stale_before == ""
    assert args.resolved_max_pages == 1


def test_direct_url_refresh_rejects_a_different_post_id():
    args = SimpleNamespace(
        collection_policy="refresh_known",
        refresh_post_id=["999"],
        refresh_stale_before="",
        max_pages=0,
    )

    with pytest.raises(operator.OperatorError, match="different"):
        operator.freeze_start_options(
            args,
            source_mode="url",
            source_target="https://www.tiktok.com/@maker/video/123",
            requested_count=1,
        )


def test_topic_refresh_freezes_automatic_cutoff_and_page_bound():
    args = SimpleNamespace(
        collection_policy="refresh_known",
        refresh_post_id=[],
        refresh_stale_before="",
        max_pages=0,
    )

    operator.freeze_start_options(
        args,
        source_mode="topic",
        source_target="music",
        requested_count=1,
    )

    assert args.refresh_stale_before
    assert args.refresh_post_id == []
    assert args.resolved_max_pages == 4


def test_automatic_restart_preparation_requires_external_trusted_receipt(
    blocked_operator_run,
):
    paths, handoff_path, handoff = blocked_operator_run
    args = SimpleNamespace(
        handoff=handoff_path,
        reason="edge_native_crash",
    )

    with pytest.raises(operator.OperatorError, match="human_action_required"):
        operator.prepare_restart_command(args, paths)
    updated = operator.load_handoff(handoff_path, paths)
    assert updated["run_id"] == handoff["run_id"]
    assert updated["restart_pending"] is False
    assert updated["restart_count"] == 0
    assert updated["recovery_epoch"] == 0


def test_restart_handoff_rejects_durably_completed_run(completed_operator_run):
    paths, handoff_path, handoff = completed_operator_run
    handoff["state"] = "browser_blocked"
    operator.write_handoff(handoff_path, handoff)

    with pytest.raises(operator.OperatorError, match="completed MUSIC AUDIT"):
        operator.prepare_restart_command(
            SimpleNamespace(handoff=handoff_path, reason="edge_native_crash"),
            paths,
        )


def test_restart_handoff_rejects_logically_complete_counter_state(
    completed_operator_run,
):
    paths, handoff_path, handoff = completed_operator_run
    conn = engage.sqlite3.connect(Path(handoff["database"]))
    try:
        conn.execute(
            "UPDATE engage_tiktok_runs SET status='browser_blocked' WHERE run_id=?",
            (handoff["run_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    handoff["state"] = "browser_blocked"
    operator.write_handoff(handoff_path, handoff)

    with pytest.raises(operator.OperatorError, match="logically complete"):
        operator.prepare_restart_command(
            SimpleNamespace(handoff=handoff_path, reason="edge_native_crash"),
            paths,
        )


def test_restart_handoff_rejects_nonbrowser_incomplete_state(
    blocked_operator_run,
):
    paths, handoff_path, handoff = blocked_operator_run
    conn = engage.sqlite3.connect(Path(handoff["database"]))
    try:
        conn.execute(
            "UPDATE engage_tiktok_runs SET status='collection_incomplete' WHERE run_id=?",
            (handoff["run_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    handoff["state"] = "collection_incomplete"
    operator.write_handoff(handoff_path, handoff)

    with pytest.raises(operator.OperatorError, match="browser/crash"):
        operator.prepare_restart_command(
            SimpleNamespace(handoff=handoff_path, reason="edge_native_crash"),
            paths,
        )


def test_resume_status_failure_persists_a_blocked_review(
    blocked_operator_run,
    monkeypatch,
):
    paths, handoff_path, handoff = blocked_operator_run

    def failed_resume(
        arguments,
        *,
        paths,
        handoff=None,
        on_process_started=None,
    ):
        if on_process_started:
            on_process_started()
        return operator.CommandResult(
            (str(paths.python), str(paths.engage_script), *arguments),
            1,
            10.0,
            {"status": "blocked"},
            "resume failed",
        )

    monkeypatch.setattr(
        operator,
        "run_engage",
        failed_resume,
    )
    monkeypatch.setattr(
        operator,
        "canonical_status",
        lambda handoff, *, paths: (_ for _ in ()).throw(
            operator.OperatorError("status unavailable")
        ),
    )

    with pytest.raises(operator.OperatorError, match="status unavailable"):
        operator.resume_command(
            SimpleNamespace(handoff=handoff_path, after_restart=False),
            paths,
        )

    updated = operator.load_handoff(handoff_path, paths)
    review = json.loads(Path(updated["review_json"]).read_text(encoding="utf-8"))
    assert updated["state"] == "blocked"
    assert updated["run_id"] == handoff["run_id"]
    assert review["task_outcome"] == "BLOCKED"
    assert review["next_action"] == "preserve_and_report_durable_blocker"


def test_resume_launch_failure_does_not_consume_epoch_attempt(
    blocked_operator_run,
    monkeypatch,
):
    paths, handoff_path, _handoff = blocked_operator_run

    def fail_before_registration(
        arguments,
        *,
        paths,
        handoff=None,
        on_process_started=None,
    ):
        return operator.CommandResult(
            (str(paths.python), str(paths.engage_script), *arguments),
            -1,
            1.0,
            {"status": "blocked", "error": "collector_launch_failed"},
            "collector_launch_failed",
            child_started=False,
        )

    monkeypatch.setattr(operator, "run_engage", fail_before_registration)

    assert (
        operator.resume_command(
            SimpleNamespace(handoff=handoff_path, after_restart=False),
            paths,
        )
        == 2
    )

    updated = operator.load_handoff(handoff_path, paths)
    assert updated["resume_attempts_by_epoch"]["0"] == 0
    assert updated["state"] == "resume_launch_failed"


def test_guarded_resume_claims_one_attempt_and_reuses_the_same_run(
    blocked_operator_run,
    monkeypatch,
):
    paths, handoff_path, handoff = blocked_operator_run
    observed_arguments = []

    def terminal_browser_resume(
        arguments,
        *,
        paths,
        handoff=None,
        on_process_started=None,
    ):
        observed_arguments.append(tuple(arguments))
        assert on_process_started is not None
        on_process_started()
        return operator.CommandResult(
            (str(paths.python), str(paths.engage_script), *arguments),
            1,
            10.0,
            {"status": "browser_blocked"},
            "Profile 7 unavailable",
            child_started=True,
        )

    monkeypatch.setattr(operator, "run_engage", terminal_browser_resume)
    monkeypatch.setattr(
        operator,
        "canonical_status",
        lambda current, *, paths: (
            operator.durable_local_run_state(current),
            operator.CommandResult(
                ("status",), 0, 1.0, {"status": "browser_blocked"}, ""
            ),
        ),
    )

    assert (
        operator.resume_command(
            SimpleNamespace(handoff=handoff_path, after_restart=False),
            paths,
        )
        == 2
    )

    updated = operator.load_handoff(handoff_path, paths)
    assert updated["project"] == handoff["project"]
    assert updated["run_id"] == handoff["run_id"]
    assert updated["resume_attempts_by_epoch"]["0"] == 1
    assert len(observed_arguments) == 1
    argv = observed_arguments[0]
    assert "resume-collect" in argv
    assert argv[argv.index("--run-id") + 1] == handoff["run_id"]
    assert argv[argv.index("--database") + 1] == handoff["database"]
    assert argv[argv.index("--master-database") + 1] == handoff["master_database"]
    assert "music-audit" not in argv
    assert "--after-restart" not in argv

    with pytest.raises(operator.OperatorError, match="resume is exhausted"):
        operator._execute_resume_command(
            SimpleNamespace(after_restart=False),
            paths,
            handoff_path=handoff_path,
            handoff=updated,
        )
    assert len(observed_arguments) == 1


def test_initial_prelaunch_failure_can_retry_the_same_handoff(monkeypatch, tmp_path):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(
            tmp_path
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )
    start_args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        max_pages=4,
        resolved_max_pages=4,
        browser_startup_timeout=120.0,
        expected_account="",
        refresh_stale_before="",
        refresh_post_id=[],
    )
    handoff_path, handoff = operator.new_handoff(
        project="operator_prelaunch_retry",
        source_mode="topic",
        source_target="music",
        requested_count=1,
        all_posts=False,
        args=start_args,
        paths=paths,
    )

    monkeypatch.setattr(
        operator.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot launch")),
    )
    assert (
        operator._execute_start_command(
            paths,
            handoff_file=handoff_path,
            handoff=handoff,
        )
        == 2
    )
    failed = operator.load_handoff(handoff_path, paths)
    assert failed["initial_attempts"] == 0
    assert failed["run_id"] == ""

    retry_calls = []

    def retry_launch(
        arguments,
        *,
        paths,
        handoff=None,
        on_process_started=None,
    ):
        retry_calls.append(tuple(arguments))
        return operator.CommandResult(
            (str(paths.python), str(paths.engage_script), *arguments),
            -1,
            1.0,
            {"status": "blocked", "error": "collector_launch_failed"},
            "collector_launch_failed",
            child_started=False,
        )

    monkeypatch.setattr(operator, "run_engage", retry_launch)
    assert (
        operator.resume_command(
            SimpleNamespace(handoff=handoff_path, after_restart=False),
            paths,
        )
        == 2
    )
    assert len(retry_calls) == 1
    assert "music-audit" in retry_calls[0]


def test_prepare_restart_rejects_collecting_even_with_claimed_crash(
    blocked_operator_run,
):
    paths, handoff_path, handoff = blocked_operator_run
    conn = engage.sqlite3.connect(Path(handoff["database"]))
    try:
        conn.execute(
            "UPDATE engage_tiktok_runs SET status='collecting' WHERE run_id=?",
            (handoff["run_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(operator.OperatorError, match="command silence never qualifies"):
        operator.prepare_restart_command(
            SimpleNamespace(handoff=handoff_path, reason="edge_native_crash"),
            paths,
        )

    updated = operator.load_handoff(handoff_path, paths)
    assert updated["restart_pending"] is False
    assert updated["restart_count"] == 0


def test_prepare_restart_rejects_a_run_with_successful_preflight(
    blocked_operator_run,
):
    paths, handoff_path, handoff = blocked_operator_run
    conn = engage.sqlite3.connect(Path(handoff["database"]))
    try:
        conn.execute(
            """
            INSERT INTO engage_tiktok_events (
                run_id, post_id, stage, event, payload_json, created_at
            ) VALUES (?, '', 'browser_preflight', 'passed', '{}', ?)
            """,
            (handoff["run_id"], "2026-08-23T05:05:13+07:00"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(operator.OperatorError, match="successful Profile 7 preflight"):
        operator.prepare_restart_command(
            SimpleNamespace(handoff=handoff_path, reason="edge_native_crash"),
            paths,
        )


def test_prepare_restart_rejects_reason_without_terminal_crash_signature(
    blocked_operator_run,
):
    paths, handoff_path, handoff = blocked_operator_run
    operator.append_ledger(
        handoff,
        action="resume-collect-start",
        stage="collection",
        paths=paths,
        next_action="canonical_resume_collect",
    )
    operator.append_ledger(
        handoff,
        action="resume-collect-result",
        stage="collection",
        paths=paths,
        exit_code=1,
        error="generic browser blocker",
        next_action="preserve",
    )

    with pytest.raises(operator.OperatorError, match="lacks a native Edge crash"):
        operator.prepare_restart_command(
            SimpleNamespace(handoff=handoff_path, reason="edge_native_crash"),
            paths,
        )


def test_after_restart_flag_cannot_replace_os_boot_proof(blocked_operator_run):
    paths, handoff_path, handoff = blocked_operator_run
    handoff["restart_count"] = 1
    handoff["restart_pending"] = True
    handoff["restart_reason"] = "edge_native_crash"
    handoff["restart_phase"] = "awaiting_reboot"
    handoff["restart_boot_marker"] = operator.current_boot_marker()
    handoff["resume_attempts_by_epoch"].setdefault("1", 0)
    handoff["state"] = "restart_pending"
    operator.write_handoff(handoff_path, handoff)

    with pytest.raises(operator.OperatorError, match="has not restarted"):
        operator.resume_command(
            SimpleNamespace(handoff=handoff_path, after_restart=True),
            paths,
        )

    updated = operator.load_handoff(handoff_path, paths)
    assert updated["restart_pending"] is True
    assert updated["recovery_epoch"] == 0
    assert updated["resume_attempts_by_epoch"]["1"] == 0


def test_cancel_legacy_false_restart_preserves_attempts_and_enables_epoch_zero(
    blocked_operator_run,
):
    paths, handoff_path, handoff = blocked_operator_run
    conn = engage.sqlite3.connect(Path(handoff["database"]))
    try:
        conn.execute(
            "UPDATE engage_tiktok_runs SET status='collecting' WHERE run_id=?",
            (handoff["run_id"],),
        )
        conn.execute(
            """
            INSERT INTO engage_tiktok_events (
                run_id, post_id, stage, event, payload_json, created_at
            ) VALUES (?, '', 'browser_preflight', 'passed', '{}', ?)
            """,
            (handoff["run_id"], "2026-08-23T05:05:13+07:00"),
        )
        conn.commit()
    finally:
        conn.close()
    operator.append_ledger(
        handoff,
        action="music-audit-start",
        stage="collection",
        paths=paths,
        next_action="capture_run_id",
    )
    handoff["restart_count"] = 1
    handoff["restart_pending"] = True
    handoff["restart_reason"] = "edge_native_crash"
    handoff["restart_phase"] = "awaiting_reboot"
    handoff["restart_boot_marker"] = {}
    handoff["recovery_epoch"] = 1
    handoff["resume_attempts_by_epoch"].setdefault("1", 0)
    handoff["state"] = "restart_pending"
    operator.write_handoff(handoff_path, handoff)
    operator.append_ledger(
        handoff,
        action="prepare-restart-handoff",
        stage="browser_recovery",
        paths=paths,
        error="edge_native_crash",
        next_action="user_restart_then_resume_after_restart",
    )
    attempts_before = dict(handoff["resume_attempts_by_epoch"])

    assert (
        operator.cancel_restart_command(
            SimpleNamespace(
                handoff=handoff_path,
                classification="premature_unverified_restart",
            ),
            paths,
        )
        == 0
    )

    updated = operator.load_handoff(handoff_path, paths)
    review = json.loads(Path(updated["review_json"]).read_text(encoding="utf-8"))
    assert updated["run_id"] == handoff["run_id"]
    assert updated["restart_pending"] is False
    assert updated["restart_count"] == 1
    assert updated["confirmed_restart_count"] == 0
    assert updated["recovery_epoch"] == 0
    assert updated["resume_attempts_by_epoch"] == attempts_before
    assert review["executor_compliance"] == "FAIL"
    assert review["status"]["status"] == "collecting"
    assert review["next_action"] == "resume_same_run_without_after_restart"
    compliance, deviations = operator.executor_compliance_summary(
        updated, operator.read_validated_ledger(updated)
    )
    assert compliance == "FAIL"
    assert any("premature restart" in value for value in deviations)


def test_cancel_restart_rejects_a_paired_terminal_browser_failure(
    blocked_operator_run,
):
    paths, handoff_path, handoff = blocked_operator_run
    handoff["restart_count"] = 1
    handoff["restart_pending"] = True
    handoff["restart_reason"] = "edge_native_crash"
    handoff["restart_phase"] = "awaiting_reboot"
    handoff["restart_boot_marker"] = operator.current_boot_marker()
    handoff["recovery_epoch"] = 1
    handoff["resume_attempts_by_epoch"].setdefault("1", 0)
    handoff["state"] = "restart_pending"
    operator.write_handoff(handoff_path, handoff)
    operator.append_ledger(
        handoff,
        action="prepare-restart-handoff",
        stage="browser_recovery",
        paths=paths,
        error="edge_native_crash",
        next_action="user_restart_then_resume_after_restart",
    )

    with pytest.raises(operator.OperatorError, match="interrupted collecting state"):
        operator.cancel_restart_command(
            SimpleNamespace(
                handoff=handoff_path,
                classification="premature_unverified_restart",
            ),
            paths,
        )

    updated = operator.load_handoff(handoff_path, paths)
    assert updated["restart_pending"] is True
    assert updated["restart_count"] == 1


def test_poll_is_read_only(blocked_operator_run, capsys):
    paths, handoff_path, handoff = blocked_operator_run
    ledger_path = Path(handoff["ledger_file"])
    handoff_before = handoff_path.read_bytes()
    ledger_before = ledger_path.read_bytes()

    assert operator.poll_command(SimpleNamespace(handoff=handoff_path), paths) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == handoff["run_id"]
    assert result["durable_status"] == "browser_blocked"
    assert result["operator_status"] == "RESUME_READY"
    assert result["same_handoff_resume_available"] is True
    assert result["resume_budget"] == {
        "epoch": 0,
        "attempts_used": 0,
        "attempts_limit": 1,
        "attempts_remaining": 1,
        "would_consume_on_child_registration": True,
    }
    action = result["safe_same_handoff_action"]
    assert action["operation"] == "guarded_resume"
    assert action["argv"][-3:] == [
        "resume",
        "--handoff",
        str(handoff_path.resolve()),
    ]
    assert action["first_browser_touching_operation"] is True
    assert action["after_restart"] is False
    assert action["replacement_project_allowed"] is False
    assert action["requires_explicit_user_direction"] is True
    assert "social_browser.py" not in " ".join(action["argv"])
    assert "--after-restart" not in action["argv"]
    assert result["replacement_project_allowed"] is False
    assert handoff_path.read_bytes() == handoff_before
    assert ledger_path.read_bytes() == ledger_before


def test_poll_requires_finalize_when_collection_is_only_durably_complete(
    completed_operator_run,
    capsys,
):
    paths, handoff_path, _handoff = completed_operator_run

    assert operator.poll_command(SimpleNamespace(handoff=handoff_path), paths) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["operator_status"] == "COLLECTION_COMPLETE_NEEDS_FINALIZE"
    assert result["next_action"] == "offline_finalize"


def test_poll_does_not_prescribe_an_exhausted_resume(
    blocked_operator_run,
    capsys,
):
    paths, handoff_path, handoff = blocked_operator_run
    handoff["resume_attempts_by_epoch"]["0"] = 1
    operator.write_handoff(handoff_path, handoff)

    assert operator.poll_command(SimpleNamespace(handoff=handoff_path), paths) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["operator_status"] == "BLOCKED"
    assert result["next_action"] == "preserve_same_run_resume_epoch_exhausted"
    assert result["same_handoff_resume_available"] is False
    assert result["resume_budget"]["attempts_remaining"] == 0
    assert result["safe_same_handoff_action"] is None
    assert result["replacement_project_allowed"] is False
    assert result["inspection_uncertain"] is False


def test_reconcile_adopts_one_fully_written_ledger_record(blocked_operator_run):
    paths, handoff_path, handoff = blocked_operator_run
    stale_handoff = handoff_path.read_bytes()
    old_sequence = int(handoff["ledger_sequence"])
    operator.append_ledger(
        handoff,
        action="status",
        stage="offline_status",
        paths=paths,
        next_action="preserve",
    )
    handoff_path.write_bytes(stale_handoff)

    recovered = operator.load_handoff(handoff_path, paths)
    operator.reconcile_ledger_head(recovered)

    updated = operator.load_handoff(handoff_path, paths)
    assert updated["ledger_sequence"] == old_sequence + 1
    assert operator.read_validated_ledger(updated)[-1]["action"] == "status"


def test_ledger_mismatch_blocks_resume_before_process_launch(
    blocked_operator_run,
    monkeypatch,
):
    paths, handoff_path, handoff = blocked_operator_run
    stale_handoff = handoff_path.read_bytes()
    for _ in range(2):
        operator.append_ledger(
            handoff,
            action="status",
            stage="offline_status",
            paths=paths,
            next_action="preserve",
        )
    handoff_path.write_bytes(stale_handoff)
    launched = []
    monkeypatch.setattr(
        operator.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launched.append(True),
    )

    with pytest.raises(operator.OperatorError, match="ledger count"):
        operator.resume_command(
            SimpleNamespace(handoff=handoff_path, after_restart=False),
            paths,
        )

    assert launched == []


def test_long_running_child_emits_safe_heartbeat(
    blocked_operator_run,
    monkeypatch,
    capsys,
):
    paths, handoff_path, handoff = blocked_operator_run
    operator.begin_execution(handoff, operation="resume")

    class FakePopen:
        def __init__(self, _argv, **_kwargs):
            self.pid = 777
            self.returncode = 0
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise operator.subprocess.TimeoutExpired("collector", timeout)
            return '{"status":"browser_blocked"}', ""

    monkeypatch.setattr(operator.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        operator,
        "process_identity",
        lambda pid, *, role: {
            "role": role,
            "pid": pid,
            "create_time": 1.0,
            "executable": str(paths.python),
        },
    )
    monkeypatch.setattr(operator, "recorded_process_alive", lambda *_a, **_k: True)

    operator.run_engage(
        operator.build_resume_arguments(handoff),
        paths=paths,
        handoff=handoff,
        heartbeat_interval=0.01,
    )

    stderr = capsys.readouterr().err
    assert "MUSIC_AUDIT_HEARTBEAT" in stderr
    assert '"instruction":"keep_waiting_do_not_restart"' in stderr
    assert "sessionid" not in stderr.casefold()


def test_automatic_start_refuses_an_unfinished_matching_run(blocked_operator_run):
    paths, _handoff_path, _handoff = blocked_operator_run
    args = SimpleNamespace(
        collection_policy="new_only",
        max_comments=20,
        resolved_max_pages=4,
        refresh_stale_before="",
        refresh_post_id=[],
        expected_account="",
    )

    with pytest.raises(operator.OperatorError, match="unfinished matching"):
        operator.refuse_automatic_duplicate_start(
            args,
            source_mode="topic",
            source_target="music",
            requested_count=1,
            all_posts=False,
            paths=paths,
        )


def test_blocked_review_is_machine_generated_and_sanitized(
    completed_operator_run,
):
    paths, _handoff_path, handoff = completed_operator_run
    review = operator.write_blocked_review(
        handoff,
        paths=paths,
        blocker=(
            "failed ws://127.0.0.1:9222/devtools/browser/"
            "01234567-89ab-cdef-0123-456789abcdef sessionid=secret"
        ),
        next_action="preserve",
    )
    markdown = Path(handoff["review_markdown"]).read_text(encoding="utf-8")

    assert review["task_outcome"] == "BLOCKED"
    assert "01234567-89ab" not in json.dumps(review)
    assert "secret" not in json.dumps(review)
    assert "Log File MUSIC AUDIT Review" in markdown


def test_active_google_instructions_contain_no_legacy_browser_recipe():
    retired = (ROOT / "docs" / "legacy" / "google 3.1 social browser instructions.md").read_text(
        encoding="utf-8"
    )
    adapter = (ROOT / "GEMINI_3_1_PRO_WORKFLOW.md").read_text(encoding="utf-8")
    skill = (
        ROOT / ".agents" / "skills" / "google-3.1-music-audit-instructions" / "SKILL.md"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "social_browser.py stop",
        "Stop-Process -Name msedge",
        "taskkill /F /IM msedge.exe",
        "--headless=new",
        "schtasks /create",
    ):
        assert forbidden not in retired
    assert "RETIRED — DO NOT EXECUTE" in retired
    assert "Music Audit Tool\\Music Audit\\TEST Tiktok Scraper Modules" in adapter
    assert "MAIN Backup for Antigravity" not in adapter
    assert "music_audit_operator.py" in skill
