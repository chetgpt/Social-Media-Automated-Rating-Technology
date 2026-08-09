import asyncio
import json

import pytest

import engage_tiktok
import tiktok_master_database as master
from engage_tiktok import (
    CollectionIncompleteError,
    StageGateError,
    collect_exact,
    connect_database,
    create_run,
    export_analysis_queue,
    normalize_creator_target,
    normalize_evidence,
    parse_args,
    run_status,
)


def creator_evidence(post_id, *, handle="maker", content_type="video"):
    record = {
        "id": str(post_id),
        "url": (
            f"https://www.tiktok.com/@{handle}/{content_type}/{post_id}"
        ),
        "username": handle,
        "content_type": content_type,
        "caption": f"Creator post {post_id}",
        "view_count": 100,
        "like_count": 10,
        "comment_count": 1,
        "share_count": 2,
        "transcript": "",
        "transcript_status": "unavailable",
        "subtitle_no_caption_reason": "not_provided",
        "comments": [{"cid": f"comment-{post_id}", "text": "Useful"}],
        "ok": True,
        "complete": True,
        "exhausted": True,
        "limit_reached": False,
        "has_more": False,
        "source": "creator-test-double",
        "discovery_method": "creator_profile",
        "discovery_source": "creator_profile_item_list",
        "metadata_method": "creator_profile_item_list",
    }
    if content_type == "photo":
        record.update(
            {
                "visual_evidence_status": "unavailable",
                "visual_evidence_terminal": True,
                "visual_slide_count": 1,
            }
        )
    return record


class ReadyPreflight:
    def __init__(self):
        self.calls = 0

    async def ensure_ready(self):
        self.calls += 1
        return {
            "reachable": True,
            "tiktok_authenticated": True,
            "cdp_url": "http://127.0.0.1:9223",
            "observed_account": "publishing-account",
            "checked_at": "2026-08-02T12:00:00+07:00",
        }


class CreatorInventoryCollector:
    def __init__(
        self,
        records,
        *,
        terminal=True,
        selected_post_ids=None,
        fail_after_inventory=False,
    ):
        self.records = list(records)
        self.terminal = terminal
        self.selected_post_ids = (
            list(selected_post_ids)
            if selected_post_ids is not None
            else [record["id"] for record in self.records]
        )
        self.fail_after_inventory = fail_after_inventory
        self.last_diagnostics = {}
        self.received = {}

    async def collect(
        self,
        *,
        topic,
        requested_count,
        max_comments,
        max_pages,
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
    ):
        del topic, max_comments, refresh_candidates
        self.received = {
            "requested_count": requested_count,
            "max_pages": max_pages,
            "existing_post_ids": tuple(existing_post_ids),
            "initial_evidence_ready_count": initial_evidence_ready_count,
            "collection_policy": collection_policy,
            "global_known_post_ids": tuple(global_known_post_ids),
            "current_run_post_ids": tuple(current_run_post_ids),
            "source_mode": source_mode,
            "creator_handle": creator_handle,
            "creator_inventory": tuple(creator_inventory),
            "creator_inventory_terminal": creator_inventory_terminal,
            "creator_selected_post_ids": tuple(creator_selected_post_ids),
        }
        assert source_mode == "creator"
        assert creator_handle == "maker"
        assert creator_inventory_callback is not None

        resolved_target = creator_inventory_callback(
            self.records,
            {
                "terminal": self.terminal,
                "terminal_verified": self.terminal,
                "inventory_complete": self.terminal,
                "selected_post_ids": self.selected_post_ids,
                "creator_identity": {
                    "handle": "maker",
                    "id": "stable-maker-id",
                    "sec_uid": "stable-maker-secuid",
                },
                "observed_at": "2026-08-02T12:01:00+07:00",
            },
        )
        self.last_diagnostics = {
            "collection_stop_reason": (
                "creator_profile_frontier_not_terminal"
                if not self.terminal
                else "exact_count_reached"
            ),
            "resolved_target": resolved_target,
        }
        if self.fail_after_inventory:
            raise CollectionIncompleteError("creator inventory is incomplete")

        returned = []
        for record in self.records:
            if record["id"] not in self.selected_post_ids:
                continue
            if (
                candidate_reserver is not None
                and not candidate_reserver(record["id"], record)
            ):
                continue
            returned.append(record)
            if record_callback(record):
                break
        return returned


def test_creator_cli_accepts_handle_or_url_and_all_cardinality():
    args = parse_args(
        [
            "collect",
            "--project",
            "creator_test",
            "--creator",
            "https://www.tiktok.com/@Maker/",
            "--all-posts",
        ]
    )
    assert args.creator == "https://www.tiktok.com/@Maker/"
    assert args.all_posts is True
    assert args.posts is None

    assert normalize_creator_target("@Maker") == (
        "maker",
        "https://www.tiktok.com/@maker",
    )
    with pytest.raises(ValueError, match="tiktok.com"):
        normalize_creator_target("https://example.com/@maker")
    with pytest.raises(ValueError, match="profile URL"):
        normalize_creator_target("https://www.tiktok.com/@maker/video/123")
    with pytest.raises(ValueError, match="exact handle"):
        normalize_creator_target("maker/video/123")


def test_captionless_photo_has_a_terminal_visual_outcome_instead_of_blocking_all():
    record = creator_evidence("900", content_type="photo")
    record["caption"] = ""
    packet, ready, issues = normalize_evidence(record, topic="creator:@maker")
    assert ready is True
    assert issues == []
    assert packet["visual_evidence_status"] == "unavailable"
    assert packet["visual_slide_count"] == 1

    record.pop("visual_evidence_status")
    _, ready_without_outcome, missing_issues = normalize_evidence(
        record,
        topic="creator:@maker",
    )
    assert ready_without_outcome is False
    assert "visual_evidence_not_terminal:missing" in missing_issues


def test_creator_run_rejects_a_collector_without_creator_protocol_before_browser(
    tmp_path,
):
    class LegacyCollector:
        async def collect(self, **kwargs):
            del kwargs
            return []

    conn = connect_database(tmp_path / "creator.sqlite")
    try:
        run_id = create_run(
            conn,
            project="creator_test",
            topic="creator:@maker",
            requested_count=0,
            max_comments=20,
            max_pages=0,
            source_mode="creator",
            creator_handle="maker",
            collect_all=True,
            run_id="creator-protocol-fence",
        )
        preflight = ReadyPreflight()
        with pytest.raises(StageGateError, match="creator-capable"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=preflight,
                    collector=LegacyCollector(),
                )
            )
        assert preflight.calls == 0
        assert run_status(conn, run_id)["status"] == "awaiting_browser"
    finally:
        conn.close()


def test_creator_all_cannot_be_combined_with_refresh_known(tmp_path):
    conn = connect_database(tmp_path / "creator.sqlite")
    try:
        with pytest.raises(ValueError, match="creator ALL"):
            create_run(
                conn,
                project="creator_test",
                topic="creator:@maker",
                requested_count=0,
                max_comments=20,
                max_pages=0,
                source_mode="creator",
                creator_handle="maker",
                collect_all=True,
                collection_policy="refresh_known",
            )
    finally:
        conn.close()


def test_creator_all_freezes_inventory_resolves_count_and_unlocks_analysis(tmp_path):
    conn = connect_database(tmp_path / "creator.sqlite")
    try:
        run_id = create_run(
            conn,
            project="creator_test",
            topic="creator:@maker",
            requested_count=0,
            max_comments=20,
            max_pages=0,
            source_mode="creator",
            creator_handle="@Maker",
            collect_all=True,
            run_id="creator-all",
        )
        collector = CreatorInventoryCollector(
            [
                creator_evidence("100"),
                creator_evidence("101", content_type="photo"),
            ]
        )

        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=ReadyPreflight(),
                collector=collector,
            )
        )

        assert result["status"] == "collection_complete"
        assert result["requested_count"] == 2
        assert result["requested"] == 2
        assert result["evidence_ready"] == 2
        assert result["source_mode"] == "creator"
        assert result["creator_handle"] == "maker"
        assert result["cardinality_mode"] == "all"
        assert bool(result["profile_inventory_terminal"]) is True
        assert result["profile_inventory_count"] == 2
        assert result["profile_inventory_hash"]
        assert collector.received["requested_count"] == 0
        assert collector.received["max_pages"] == 0

        rows = conn.execute(
            """
            SELECT post_id, url, evidence_json
            FROM engage_tiktok_posts
            WHERE run_id=?
            ORDER BY post_id
            """,
            (run_id,),
        ).fetchall()
        assert [row["post_id"] for row in rows] == ["100", "101"]
        photo_packet = json.loads(rows[1]["evidence_json"])
        assert photo_packet["content_type"] == "photo"
        assert photo_packet["url"].endswith("/@maker/photo/101")
        assert photo_packet["creator_source_validation"]["matched"] is True

        queue_path = tmp_path / "analysis.jsonl"
        assert export_analysis_queue(conn, run_id, queue_path) == 2
        assert len(queue_path.read_text(encoding="utf-8").splitlines()) == 2
    finally:
        conn.close()


def test_creator_nonterminal_inventory_is_durable_but_analysis_remains_locked(
    tmp_path,
):
    conn = connect_database(tmp_path / "creator.sqlite")
    try:
        run_id = create_run(
            conn,
            project="creator_test",
            topic="creator:@maker",
            requested_count=0,
            max_comments=20,
            max_pages=0,
            source_mode="creator",
            creator_handle="maker",
            collect_all=True,
            run_id="creator-partial",
        )
        collector = CreatorInventoryCollector(
            [creator_evidence("100")],
            terminal=False,
            fail_after_inventory=True,
        )

        with pytest.raises(CollectionIncompleteError, match="target unresolved"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=ReadyPreflight(),
                    collector=collector,
                )
            )

        status = run_status(conn, run_id)
        assert status["status"] == "collection_incomplete"
        assert bool(status["profile_inventory_terminal"]) is False
        assert status["profile_inventory_count"] == 1
        assert status["requested_count"] == 0
        with pytest.raises(StageGateError, match="collection is not complete"):
            export_analysis_queue(conn, run_id, tmp_path / "blocked.jsonl")
    finally:
        conn.close()


def test_fixed_creator_request_reports_clean_incomplete_when_profile_is_too_small(
    tmp_path,
):
    conn = connect_database(tmp_path / "creator.sqlite")
    try:
        run_id = create_run(
            conn,
            project="creator_test",
            topic="creator:@maker",
            requested_count=2,
            max_comments=20,
            max_pages=0,
            source_mode="creator",
            creator_handle="maker",
            run_id="creator-fixed-exhausted",
        )
        with pytest.raises(CollectionIncompleteError, match="1/2"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=ReadyPreflight(),
                    collector=CreatorInventoryCollector(
                        [creator_evidence("100")]
                    ),
                )
            )
        status = run_status(conn, run_id)
        assert status["status"] == "collection_incomplete"
        assert status["evidence_ready"] == 1
        assert bool(status["profile_inventory_terminal"]) is True
    finally:
        conn.close()


def test_creator_new_only_receives_legacy_comment_targets_in_global_fence(
    tmp_path,
):
    master_path = tmp_path / "master.sqlite"
    conn = connect_database(
        tmp_path / "creator.sqlite",
        master_database=master_path,
        sync_master=False,
    )
    try:
        attempt_id = master.register_publication_claim(
            conn,
            "master",
            account="publishing-account",
            post_id="100",
            publication_id="legacy-comment-only",
            source_path="legacy.sqlite",
        )
        master.mark_submit_intent(conn, "master", attempt_id)
        master.register_publication_outcome(
            conn,
            "master",
            attempt_id=attempt_id,
            outcome="failed",
            error="legacy outcome uncertain",
        )
        conn.commit()
        assert "100" not in master.known_post_ids(conn, "master")

        run_id = create_run(
            conn,
            project="creator_test",
            topic="creator:@maker",
            requested_count=1,
            max_comments=20,
            max_pages=0,
            source_mode="creator",
            creator_handle="maker",
            master_database=str(master_path.resolve()),
            run_id="creator-comment-fence",
        )
        collector = CreatorInventoryCollector([creator_evidence("101")])
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=ReadyPreflight(),
                collector=collector,
            )
        )

        assert result["status"] == "collection_complete"
        assert "100" in collector.received["global_known_post_ids"]
    finally:
        conn.close()


def test_creator_wrong_owner_evidence_never_counts(tmp_path):
    conn = connect_database(tmp_path / "creator.sqlite")
    try:
        run_id = create_run(
            conn,
            project="creator_test",
            topic="creator:@maker",
            requested_count=1,
            max_comments=20,
            max_pages=0,
            source_mode="creator",
            creator_handle="maker",
            run_id="creator-owner-fence",
        )
        inventory_record = creator_evidence("100")
        class WrongOwnerCollector:
            def __init__(self):
                self.last_diagnostics = {}

            async def collect(
                self,
                *,
                topic,
                requested_count,
                max_comments,
                max_pages,
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
            ):
                del (
                    topic,
                    requested_count,
                    max_comments,
                    max_pages,
                    existing_post_ids,
                    initial_evidence_ready_count,
                    collection_policy,
                    global_known_post_ids,
                    current_run_post_ids,
                    refresh_candidates,
                    candidate_reserver,
                    creator_inventory,
                    creator_inventory_terminal,
                    creator_selected_post_ids,
                )
                assert source_mode == "creator"
                assert creator_handle == "maker"
                resolved = creator_inventory_callback(
                    [inventory_record],
                    {
                        "terminal": True,
                        "terminal_verified": True,
                        "inventory_complete": True,
                        "selected_post_ids": ["100"],
                        "creator_identity": {"handle": "maker"},
                    },
                )
                assert resolved == 1
                changed = creator_evidence("100", handle="other")
                changed["url"] = "https://www.tiktok.com/@maker/video/100"
                record_callback(changed)
                self.last_diagnostics = {
                    "collection_stop_reason": (
                        "creator_profile_inventory_exhausted"
                    )
                }
                return [changed]

        collector = WrongOwnerCollector()

        with pytest.raises(CollectionIncompleteError, match="0/1"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=ReadyPreflight(),
                    collector=collector,
                )
            )

        row = conn.execute(
            """
            SELECT evidence_ready, collection_error
            FROM engage_tiktok_posts
            WHERE run_id=? AND post_id='100'
            """,
            (run_id,),
        ).fetchone()
        assert row["evidence_ready"] == 0
        assert "creator_source_identity_not_matched" in row["collection_error"]
    finally:
        conn.close()


def test_same_creator_post_outside_frozen_selection_is_rejected(tmp_path):
    inventory_record = creator_evidence("100")

    # Give the override an explicit creator protocol; **kwargs-only collectors
    # are deliberately rejected before browser access.
    class ExplicitSubstitutionCollector:
        last_diagnostics = {}

        async def collect(
            self,
            *,
            topic,
            requested_count,
            max_comments,
            max_pages,
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
        ):
            del (
                topic,
                requested_count,
                max_comments,
                max_pages,
                existing_post_ids,
                initial_evidence_ready_count,
                collection_policy,
                global_known_post_ids,
                current_run_post_ids,
                refresh_candidates,
                candidate_reserver,
                creator_inventory,
                creator_inventory_terminal,
                creator_selected_post_ids,
            )
            assert source_mode == "creator"
            assert creator_handle == "maker"
            creator_inventory_callback(
                [inventory_record],
                {
                    "terminal": True,
                    "terminal_verified": True,
                    "inventory_complete": True,
                    "selected_post_ids": ["100"],
                    "creator_identity": {"handle": "maker"},
                },
            )
            record_callback(creator_evidence("101"))
            return []

    conn = connect_database(tmp_path / "creator.sqlite")
    try:
        run_id = create_run(
            conn,
            project="creator_test",
            topic="creator:@maker",
            requested_count=1,
            max_comments=20,
            max_pages=0,
            source_mode="creator",
            creator_handle="maker",
            run_id="creator-substitution-fence",
        )
        with pytest.raises(StageGateError, match="outside the frozen"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=ReadyPreflight(),
                    collector=ExplicitSubstitutionCollector(),
                )
            )
        assert run_status(conn, run_id)["status"] == "collection_failed"
        assert conn.execute(
            "SELECT COUNT(*) FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()
