import asyncio
import json

import pytest

import engage_tiktok
from tiktok_master_database import audit_report_for_run as master_audit_report_for_run
from engage_tiktok import (
    StageGateError,
    audit_report_for_run,
    authorize_response,
    collect_exact,
    connect_database,
    create_run,
    export_analysis_queue,
    export_draft_queue,
    export_reclassification_queue,
    export_review_queue,
    handoff_publication,
    import_analysis_results,
    import_draft_results,
    import_reclassification_results,
    import_review_results,
    main,
    parse_args,
    present_response,
    run_status,
)


def _write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _evidence(post_id):
    return {
        "id": str(post_id),
        "url": f"https://www.tiktok.com/@auditcreator/video/{post_id}",
        "username": "auditcreator",
        "caption": f"Evidence-grounded audit post {post_id}",
        "view_count": 1000,
        "like_count": 100,
        "comment_count": 1,
        "share_count": 10,
        "transcript": "A short transcript.",
        "transcript_status": "ok",
        "comments": [{"cid": f"comment-{post_id}", "text": "Useful context"}],
        "ok": True,
        "complete": True,
        "exhausted": True,
        "limit_reached": False,
        "has_more": False,
        "source": "audit-test-double",
        "discovery_method": "test",
        "discovery_source": "test",
        "metadata_method": "test",
        "published_at": "2026-08-01T00:00:00+00:00",
    }


class _Preflight:
    async def ensure_ready(self):
        return {
            "reachable": True,
            "tiktok_authenticated": True,
            "observed_account": "commentingaccount",
            "checked_at": "2026-08-09T10:00:00+07:00",
        }


class _Collector:
    def __init__(self, records):
        self.records = list(records)

    async def collect(
        self,
        *,
        record_callback=None,
        existing_post_ids=(),
        initial_evidence_ready_count=0,
        candidate_reserver=None,
        **_kwargs,
    ):
        del existing_post_ids, initial_evidence_ready_count
        returned = []
        for record in self.records:
            if candidate_reserver is not None and not candidate_reserver(
                str(record["id"]), record
            ):
                continue
            returned.append(record)
            if record_callback is not None and record_callback(record):
                break
        return returned


def _collected_audit(conn, *, count=2, run_id="audit-run"):
    create_run(
        conn,
        project="audit-test",
        topic="audit topic",
        requested_count=count,
        max_comments=20,
        max_pages=10,
        workflow="audit",
        mode="shadow",
        music_catalogs=(),
        run_id=run_id,
    )
    asyncio.run(
        collect_exact(
            conn,
            run_id=run_id,
            preflight=_Preflight(),
            collector=_Collector(_evidence(index) for index in range(1, count + 1)),
        )
    )
    return run_id


def _analysis_result(task, *, post_quality, conversation, response_type):
    positive = response_type == "positive_support"
    return {
        "post_id": task["post_id"],
        "evidence_hash": task["evidence_hash"],
        "analysis": {
            "summary": "The stored evidence was assessed as a complete post packet.",
            "post_quality_score": post_quality,
            "conversation_value_score": conversation,
            "confidence": 95,
            "positive_eligible": positive,
            "response_type": response_type,
            "decision_reason": "The decision follows the caption, transcript, and comments.",
            "novel_value": "The portfolio comparison adds aggregate context.",
            "strength": "The claim is specific and grounded in stored evidence.",
            "recommendation": "Use the result as an internal portfolio signal.",
            "evidence_refs": ["caption", "transcript", "comments"],
            "response_objective": "",
            "response_rationale": "",
            "correction_target": "",
            "blocking_risk_flags": [],
            "skip_reason": "insufficient_response_opportunity" if not positive else "",
        },
    }


def _complete_two_post_audit(conn, tmp_path, *, run_id="audit-run"):
    _collected_audit(conn, count=2, run_id=run_id)
    queue_path = tmp_path / f"{run_id}-analysis-queue.jsonl"
    assert export_analysis_queue(conn, run_id, queue_path) == 2
    tasks = _read_jsonl(queue_path)
    results_path = tmp_path / f"{run_id}-analysis-results.jsonl"
    _write_jsonl(
        results_path,
        [
            _analysis_result(
                tasks[0],
                post_quality=80,
                conversation=90,
                response_type="positive_support",
            ),
            _analysis_result(
                tasks[1],
                post_quality=40,
                conversation=20,
                response_type="skip",
            ),
        ],
    )
    assert import_analysis_results(
        conn,
        run_id,
        results_path,
        actor="codex-audit-analysis",
    ) == {"applied": 2}
    return run_id


def test_audit_is_a_shadow_only_first_class_cli_workflow(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        with pytest.raises(ValueError, match="AUDIT workflow cannot publish"):
            create_run(
                conn,
                project="audit-live",
                topic="topic",
                requested_count=1,
                max_comments=10,
                max_pages=3,
                workflow="audit",
                mode="live",
            )
        args = parse_args(
            [
                "collect",
                "--project",
                "audit",
                "--topic",
                "coffee",
                "--posts",
                "3",
                "--workflow",
                "audit",
            ]
        )
        assert args.workflow == "audit"
        report_args = parse_args(
            ["--database", str(tmp_path / "state.sqlite"), "audit-report", "--run-id", "x"]
        )
        assert report_args.command == "audit-report"
    finally:
        conn.close()


def test_audit_requires_exact_collection_before_analysis(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        create_run(
            conn,
            project="audit-exact",
            topic="topic",
            requested_count=2,
            max_comments=10,
            max_pages=3,
            workflow="audit",
            run_id="audit-exact",
        )
        with pytest.raises(StageGateError, match="exact-count gate failed: 0/2"):
            export_analysis_queue(
                conn,
                "audit-exact",
                tmp_path / "blocked.jsonl",
            )
    finally:
        conn.close()


def test_audit_final_analysis_stores_deterministic_portfolio_report(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = _complete_two_post_audit(conn, tmp_path)
        status = run_status(conn, run_id)
        assert status["status"] == "audit_complete"
        assert status["analyzed"] == 2
        assert status["skipped"] == 1
        assert status["drafted"] == 0
        assert status["reviewed"] == 0
        assert status["stored"] == 0
        assert status["authorized"] == 0
        assert status["published"] == 0

        updated_before = conn.execute(
            "SELECT updated_at FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        first = audit_report_for_run(conn, run_id)
        second = audit_report_for_run(conn, run_id)
        assert first == second
        assert conn.execute(
            "SELECT updated_at FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == updated_before
        assert first["report_hash"] == engage_tiktok.json_hash(first["report"])
        report = first["report"]
        assert "generated_at" not in report
        assert report["schema_version"] == "tiktok-audit-report-v1"
        assert report["rubric_version"] == "topic-portfolio-v1"
        assert report["analysis_set_hash"] == first["analysis_set_hash"]
        assert report["requested_count"] == report["analyzed_count"] == 2
        assert report["ratings"]["content_quality"]["formatted"] == "6/10"
        assert report["ratings"]["engage_suitability"]["formatted"] == "5.9/10"
        assert report["ratings"]["conversation_value"]["formatted"] == "5.5/10"
        assert report["ratings"]["rateable_content_quality"]["formatted"] == "8/10"
        assert report["ratings"]["rateable_content_quality"]["coverage_percent"] == 50.0
        assert report["response_type_distribution"]["positive_support"] == 1
        assert report["response_type_distribution"]["skip"] == 1
        assert report["portfolio_subject"]["not_person_rating"] is True
        assert report["portfolio_subject"]["provisional_internal"] is True
        assert status["audit_report"]["report_hash"] == first["report_hash"]
    finally:
        conn.close()


def test_partial_audit_import_does_not_create_report(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = _collected_audit(conn, count=2, run_id="partial-audit")
        queue = tmp_path / "queue.jsonl"
        export_analysis_queue(conn, run_id, queue)
        tasks = _read_jsonl(queue)
        first_result = tmp_path / "first.jsonl"
        _write_jsonl(
            first_result,
            [
                _analysis_result(
                    tasks[0],
                    post_quality=80,
                    conversation=90,
                    response_type="positive_support",
                )
            ],
        )
        import_analysis_results(
            conn,
            run_id,
            first_result,
            actor="codex-audit-analysis",
        )
        assert run_status(conn, run_id)["status"] == "analysis_pending"
        assert run_status(conn, run_id)["audit_report"] is None
        with pytest.raises(StageGateError, match="not finalized"):
            audit_report_for_run(conn, run_id)
    finally:
        conn.close()


def test_terminal_audit_analysis_and_report_are_one_atomic_batch(tmp_path, monkeypatch):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = _collected_audit(conn, count=1, run_id="atomic-audit")
        queue = tmp_path / "atomic-queue.jsonl"
        export_analysis_queue(conn, run_id, queue)
        task = _read_jsonl(queue)[0]
        results = tmp_path / "atomic-results.jsonl"
        _write_jsonl(
            results,
            [
                _analysis_result(
                    task,
                    post_quality=80,
                    conversation=90,
                    response_type="positive_support",
                )
            ],
        )

        def fail_report(*_args, **_kwargs):
            raise StageGateError("forced aggregate failure")

        monkeypatch.setattr(engage_tiktok, "_build_audit_report", fail_report)
        with pytest.raises(StageGateError, match="forced aggregate failure"):
            import_analysis_results(
                conn,
                run_id,
                results,
                actor="codex-audit-analysis",
            )
        post = conn.execute(
            "SELECT status, analysis_hash FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert tuple(post) == ("collected", "")
        assert conn.execute(
            "SELECT COUNT(*) FROM engage_tiktok_audit_reports WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_audit_hard_stops_every_response_and_publication_stage(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = _complete_two_post_audit(conn, tmp_path, run_id="gated-audit")
        missing = tmp_path / "not-read.jsonl"
        operations = [
            lambda: export_reclassification_queue(conn, run_id, tmp_path / "r.jsonl"),
            lambda: import_reclassification_results(
                conn, run_id, missing, actor="codex-audit-analysis"
            ),
            lambda: export_draft_queue(conn, run_id, tmp_path / "d.jsonl"),
            lambda: import_draft_results(
                conn, run_id, missing, actor="codex-audit-drafter"
            ),
            lambda: export_review_queue(conn, run_id, tmp_path / "v.jsonl"),
            lambda: import_review_results(
                conn, run_id, missing, actor="codex-audit-reviewer"
            ),
            lambda: present_response(
                conn, run_id, "1", presented_to="Human Operator"
            ),
            lambda: authorize_response(
                conn,
                run_id,
                "1",
                authorized_by="Human Operator",
                expected_draft_hash="x",
                expected_review_hash="x",
                expected_presentation_hash="x",
                approval_token="x",
            ),
            lambda: handoff_publication(conn, run_id, "1"),
        ]
        for operation in operations:
            with pytest.raises(StageGateError, match="only for ENGAGE"):
                operation()
    finally:
        conn.close()


def test_listen_cannot_bypass_collection_only_rule_with_direct_analysis_import(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = create_run(
            conn,
            project="listen",
            topic="topic",
            requested_count=1,
            max_comments=10,
            max_pages=3,
            workflow="listen",
            music_catalogs=(),
            run_id="listen-direct-import",
        )
        asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=_Preflight(),
                collector=_Collector([_evidence("1")]),
            )
        )
        direct = tmp_path / "direct.jsonl"
        _write_jsonl(direct, [])
        with pytest.raises(StageGateError, match="AUDIT/ENGAGE"):
            import_analysis_results(
                conn,
                run_id,
                direct,
                actor="codex-analysis",
            )
    finally:
        conn.close()


def test_empty_terminal_creator_audit_produces_null_ratings(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = create_run(
            conn,
            project="empty-creator-audit",
            topic="creator:@emptycreator",
            requested_count=0,
            max_comments=10,
            max_pages=0,
            workflow="audit",
            source_mode="creator",
            creator_handle="emptycreator",
            creator_profile_url="https://www.tiktok.com/@emptycreator",
            collect_all=True,
            run_id="empty-creator-audit",
        )
        identity = {
            "handle": "emptycreator",
            "profile_url": "https://www.tiktok.com/@emptycreator",
        }
        snapshot = {
            "creator_handle": "emptycreator",
            "creator_identity": identity,
            "terminal": True,
            "candidates": [],
            "selected_post_ids": [],
        }
        conn.execute(
            """
            UPDATE engage_tiktok_runs SET
                status='collection_complete', creator_identity_json=?,
                creator_inventory_json='[]', creator_selected_post_ids_json='[]',
                profile_inventory_hash=?, profile_inventory_terminal=1,
                profile_inventory_count=0
            WHERE run_id=?
            """,
            (json.dumps(identity, sort_keys=True, separators=(",", ":")), engage_tiktok.json_hash(snapshot), run_id),
        )
        conn.commit()
        queue = tmp_path / "empty.jsonl"
        assert export_analysis_queue(conn, run_id, queue) == 0
        result = audit_report_for_run(conn, run_id)
        assert result["report"]["analyzed_count"] == 0
        assert result["report"]["ratings"]["content_quality"]["value_10"] is None
        assert result["report"]["coverage"]["analysis_coverage_percent"] == 100.0
        assert run_status(conn, run_id)["status"] == "audit_complete"
    finally:
        conn.close()


def test_audit_report_tampering_fails_closed(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = _complete_two_post_audit(conn, tmp_path, run_id="tamper-audit")
        conn.execute(
            "UPDATE engage_tiktok_audit_reports SET report_json='{}' WHERE run_id=?",
            (run_id,),
        )
        conn.commit()
        with pytest.raises(StageGateError, match="immutable analysis set"):
            audit_report_for_run(conn, run_id)
    finally:
        conn.close()


def test_audit_report_synchronizes_to_workspace_master_registry(tmp_path):
    local_database = tmp_path / "local.sqlite"
    master_database = tmp_path / "master.sqlite"
    conn = connect_database(local_database, master_database=master_database)
    try:
        run_id = _complete_two_post_audit(conn, tmp_path, run_id="master-audit")
        local = audit_report_for_run(conn, run_id)
        engage_tiktok._register_master_run_state(conn, run_id)
        schema = engage_tiktok._master_database_schema(conn)
        assert schema is not None
        master = master_audit_report_for_run(
            conn,
            schema,
            run_id=run_id,
            source_path=local_database,
        )
        assert master is not None
        assert master["report_hash"] == local["report_hash"]
        assert master["report"] == local["report"]
    finally:
        conn.close()


def test_audit_handoff_cli_rejects_before_browser_revalidation(tmp_path, monkeypatch):
    local_database = tmp_path / "local.sqlite"
    master_database = tmp_path / "master.sqlite"
    conn = connect_database(local_database)
    try:
        create_run(
            conn,
            project="offline-audit",
            topic="topic",
            requested_count=1,
            max_comments=10,
            max_pages=3,
            workflow="audit",
            run_id="offline-audit",
        )
    finally:
        conn.close()

    called = False

    async def forbidden_revalidation(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("AUDIT must not touch Profile 7 after collection")

    monkeypatch.setattr(engage_tiktok, "revalidate_run_browser", forbidden_revalidation)
    assert main(
        [
            "--database",
            str(local_database),
            "--master-database",
            str(master_database),
            "handoff",
            "--run-id",
            "offline-audit",
            "--post-id",
            "1",
        ]
    ) == 1
    assert called is False
