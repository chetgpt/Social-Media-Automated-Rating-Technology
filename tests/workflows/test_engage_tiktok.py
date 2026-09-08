import asyncio
import io
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import engage_tiktok
from tiktok_publication_adapter import (
    claim_publication,
    load_approved_publication,
    mark_publication_submit_intent,
    store_receipt,
)
from engage_tiktok import (
    BROWSER_REVALIDATION_COMMANDS,
    BrowserPreflightError,
    CollectionIncompleteError,
    SocialBrowserPreflight,
    StageGateError,
    TikTokBrowserCollector,
    authorize_response,
    canonical_analysis_score,
    collect_exact,
    compact_ai_evidence_projection,
    connect_database,
    create_run,
    deterministic_public_rating,
    export_analysis_queue,
    export_draft_queue,
    export_review_queue,
    handoff_publication,
    import_analysis_results,
    import_draft_results,
    import_review_results,
    normalize_evidence,
    parse_args,
    publisher_dry_run_command,
    present_response,
    run_status,
)


def write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_console_json_result_is_lossless_on_legacy_windows_stdout(monkeypatch):
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)

    payload = {"caption": "AI builder 😂 — alat yang berguna"}
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    console.flush()

    encoded = raw.getvalue().decode("cp1252")
    assert "\\ud83d\\ude02" in encoded
    assert json.loads(encoded) == payload


def evidence(post_id, *, ready=True):
    record = {
        "id": str(post_id),
        "url": f"https://www.tiktok.com/@creator/video/{post_id}",
        "username": "creator",
        "caption": f"Useful post {post_id}",
        "view_count": 100,
        "like_count": 10,
        "comment_count": 1,
        "share_count": 2,
        "transcript": "",
        "transcript_status": "unavailable",
        "subtitle_no_caption_reason": "not_provided",
        "comments": [{"cid": f"comment-{post_id}", "text": "Helpful question"}],
        "ok": True,
        "complete": True,
        "exhausted": True,
        "limit_reached": False,
        "has_more": False,
        "source": "test-double",
        "discovery_method": "test",
        "discovery_source": "test",
        "metadata_method": "test",
    }
    if not ready:
        record["transcript_status"] = ""
    return record


class RecordingPreflight:
    def __init__(self, calls, *, fail=False, account="creator"):
        self.calls = calls
        self.fail = fail
        self.account = account

    async def ensure_ready(self):
        self.calls.append("preflight")
        if self.fail:
            raise BrowserPreflightError("TikTok login required")
        return {
            "reachable": True,
            "tiktok_authenticated": True,
            "cdp_url": "http://127.0.0.1:9223",
            "observed_account": self.account,
            "checked_at": "2026-07-26T10:00:00+07:00",
        }


class RecordingCollector:
    def __init__(self, calls, records):
        self.calls = calls
        self.records = records
        self.arguments = None

    async def collect(self, **kwargs):
        assert self.calls == ["preflight"]
        self.calls.append("collector")
        self.arguments = kwargs
        return list(self.records)


class CheckpointingCollector:
    def __init__(self, calls, records, *, error=None, stop_when_exact=True):
        self.calls = calls
        self.records = list(records)
        self.error = error
        self.stop_when_exact = stop_when_exact
        self.existing_post_ids = ()
        self.initial_evidence_ready_count = None

    async def collect(
        self,
        *,
        topic,
        requested_count,
        max_comments,
        max_pages,
        record_callback=None,
        existing_post_ids=(),
        initial_evidence_ready_count=0,
        collection_policy="new_only",
        global_known_post_ids=(),
        current_run_post_ids=(),
        refresh_candidates=(),
        candidate_reserver=None,
    ):
        assert self.calls == ["preflight"]
        self.calls.append("collector")
        assert record_callback is not None
        self.existing_post_ids = tuple(existing_post_ids)
        self.initial_evidence_ready_count = initial_evidence_ready_count
        returned = []
        for record in self.records:
            post_id = engage_tiktok.extract_post_id(record)
            if (
                candidate_reserver is not None
                and post_id
                and not candidate_reserver(post_id, record)
            ):
                continue
            returned.append(record)
            exact = record_callback(record)
            if exact and self.stop_when_exact:
                break
        if self.error is not None:
            raise self.error
        return returned


def new_run(
    conn,
    *,
    requested=1,
    mode="shadow",
    run_id="run-1",
    project="engage_test",
):
    return create_run(
        conn,
        project=project,
        topic="coffee",
        requested_count=requested,
        max_comments=20,
        max_pages=10,
        mode=mode,
        music_catalogs=(),
        run_id=run_id,
    )


def test_real_preflight_auto_starts_and_uses_visible_profile7_account_link(
    tmp_path,
    monkeypatch,
):
    calls = []
    state_path = tmp_path / "social_browser" / "state.json"
    designation = {
        "user_data_dir": str(tmp_path / "Edge User Data"),
        "profile_directory": "Profile 7",
        "designation_id": "profile7-designation",
        "mode": "existing_profile_attach",
    }
    state = {
        "mode": "existing_profile_attach",
        "cdp_url": "ws://127.0.0.1:9222/devtools/browser/profile7",
        "profile_directory": "Profile 7",
        "profile_designation_id": "profile7-designation",
    }

    def fake_ensure(received_state_path, *, startup_timeout):
        calls.append(("start", Path(received_state_path), startup_timeout))
        return {"designation": designation, "state": state}

    async def fake_status(cdp_url, **kwargs):
        calls.append(("status", cdp_url))
        assert kwargs["expected_profile"] == Path(designation["user_data_dir"])
        assert kwargs["expected_profile_directory"] == "Profile 7"
        return {
            "reachable": True,
            "profile": {
                "verified": True,
                "expected_profile_directory": "Profile 7",
            },
            "platforms": {
                "tiktok": {"authenticated": True},
                "instagram": {"authenticated": True},
                "facebook": {"authenticated": True},
                "x": {"authenticated": True},
            },
        }

    async def fake_verified_context(received_browser, received_designation):
        assert received_designation == designation
        return (
            received_browser.contexts[0],
            {
                "verified": True,
                "verification_method": "edge_version_profile_path",
            },
        )

    async def fake_authentication(received_context, platform):
        assert received_context is FakeBrowser.contexts[0]
        assert platform == "tiktok"
        return {"authenticated": True}

    class EmptyLocator:
        async def count(self):
            return 0

    class ProfileCandidate:
        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            assert name == "href"
            return "/@profile7_user"

        def locator(self, selector):
            return EmptyLocator()

    class ProfileLocator:
        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return ProfileCandidate()

    class FakePage:
        closed = False

        def locator(self, selector):
            if selector == 'a[data-e2e="profile-icon"]':
                return ProfileLocator()
            return EmptyLocator()

        async def goto(self, url, **kwargs):
            calls.append(("goto", url))

        async def evaluate(self, script):
            return ""

        async def wait_for_timeout(self, milliseconds):
            return None

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = FakePage()

    class FakeContext:
        async def new_page(self):
            return page

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        async def connect_over_cdp(self, cdp_url):
            calls.append(("connect", cdp_url))
            return FakeBrowser()

    class FakePlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    import playwright.async_api
    import social_browser

    monkeypatch.setattr(
        engage_tiktok,
        "ensure_profile7_social_browser",
        fake_ensure,
    )
    monkeypatch.setattr(social_browser, "browser_status", fake_status)
    monkeypatch.setattr(
        social_browser,
        "verified_profile_context",
        fake_verified_context,
    )
    monkeypatch.setattr(
        social_browser,
        "platform_authentication",
        fake_authentication,
    )
    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: FakePlaywrightContext(),
    )

    result = asyncio.run(
        SocialBrowserPreflight(
            state_path=state_path,
            startup_timeout=45,
        ).ensure_ready()
    )

    assert [call[0] for call in calls] == ["start", "status", "connect", "goto"]
    assert result["observed_account"] == "profile7_user"
    assert result["expected_account"] == ""
    assert result["profile"]["profile_directory"] == "Profile 7"
    assert "cdp_url" not in result
    assert page.closed is True


def test_real_preflight_rejects_non_profile7_connection_before_navigation(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "social_browser" / "state.json"
    monkeypatch.setattr(
        engage_tiktok,
        "ensure_profile7_social_browser",
        lambda *args, **kwargs: {
            "designation": {
                "user_data_dir": str(tmp_path / "Edge User Data"),
                "profile_directory": "Profile 7",
                "designation_id": "profile7-designation",
                "mode": "existing_profile_attach",
            },
            "state": {
                "mode": "existing_profile_attach",
                "cdp_url": "ws://127.0.0.1:9222/devtools/browser/wrong",
                "profile_directory": "Profile 7",
                "profile_designation_id": "profile7-designation",
            },
        },
    )

    async def wrong_status(*args, **kwargs):
        return {
            "reachable": True,
            "profile": {
                "verified": True,
                "expected_profile_directory": "Default",
            },
            "platforms": {"tiktok": {"authenticated": True}},
        }

    import social_browser

    monkeypatch.setattr(social_browser, "browser_status", wrong_status)

    with pytest.raises(BrowserPreflightError, match="not verified Edge Profile 7"):
        asyncio.run(SocialBrowserPreflight(state_path=state_path).ensure_ready())


def test_profile7_launcher_retries_once_in_the_same_preflight(
    tmp_path,
    monkeypatch,
):
    import social_browser

    calls = []

    def flaky_launcher(runtime_dir, *, startup_timeout, open_tabs):
        calls.append((Path(runtime_dir), startup_timeout, open_tabs))
        if len(calls) == 1:
            raise RuntimeError("live-debugging enable failed")
        return {
            "designation": {
                "profile_directory": "Profile 7",
                "mode": "existing_profile_attach",
            },
            "state": {
                "profile_directory": "Profile 7",
                "mode": "existing_profile_attach",
                "cdp_url": "ws://127.0.0.1:9222/devtools/browser/profile7",
            },
        }

    monkeypatch.setattr(
        social_browser,
        "ensure_engage_profile7_browser",
        flaky_launcher,
    )
    session = engage_tiktok.ensure_profile7_social_browser(
        tmp_path / "social_browser" / "state.json",
        startup_timeout=45,
        startup_attempts=2,
        retry_delay=0,
    )

    assert len(calls) == 2
    assert session["startup_attempts"] == 2
    assert session["startup_duration_ms"] >= 0
    assert session["state"]["profile_directory"] == "Profile 7"


def collected_run(
    conn,
    *,
    tmp_path,
    mode="live",
    requested=1,
    run_id="run-1",
    project="engage_test",
    account="creator",
):
    run_id = new_run(
        conn,
        requested=requested,
        mode=mode,
        run_id=run_id,
        project=project,
    )
    calls = []
    records = [evidence(index) for index in range(1, requested + 1)]
    asyncio.run(
        collect_exact(
            conn,
            run_id=run_id,
            preflight=RecordingPreflight(calls, account=account),
            collector=RecordingCollector(calls, records),
        )
    )
    return run_id


def advance_to_reviewed(
    conn,
    tmp_path,
    *,
    mode="live",
    run_id="run-1",
    project="engage_test",
    account="creator",
):
    run_id = collected_run(
        conn,
        tmp_path=tmp_path,
        mode=mode,
        run_id=run_id,
        project=project,
        account=account,
    )

    analysis_queue = tmp_path / f"{run_id}-analysis.jsonl"
    assert export_analysis_queue(conn, run_id, analysis_queue) == 1
    analysis_task = read_jsonl(analysis_queue)[0]
    analysis_results = tmp_path / f"{run_id}-analysis-results.jsonl"
    write_jsonl(
        analysis_results,
        [
            {
                "post_id": analysis_task["post_id"],
                "evidence_hash": analysis_task["evidence_hash"],
                "analysis": {
                    "summary": "The post is useful and the discussion adds value.",
                    "post_quality_score": 80,
                    "conversation_value_score": 100,
                    "confidence": 90,
                    "positive_eligible": True,
                    "decision_reason": (
                        "A specific constructive recommendation adds value."
                    ),
                    "novel_value": "It asks for a concrete supporting source.",
                    "strength": "The explanation is clear and useful.",
                    "recommendation": "Add one concrete supporting source.",
                    "evidence_refs": ["caption", "comment-1"],
                    "skip_reason": "",
                },
            }
        ],
    )
    with pytest.raises(StageGateError, match="not an external API"):
        import_analysis_results(
            conn,
            run_id,
            analysis_results,
            actor="external-llm-api",
        )
    assert import_analysis_results(
        conn,
        run_id,
        analysis_results,
        actor="codex-analysis",
    ) == {"applied": 1}

    with pytest.raises(StageGateError, match="drafted"):
        export_review_queue(conn, run_id, tmp_path / "too-early-review.jsonl")

    draft_queue = tmp_path / f"{run_id}-draft.jsonl"
    assert export_draft_queue(conn, run_id, draft_queue) == 1
    draft_task = read_jsonl(draft_queue)[0]
    draft_results = tmp_path / f"{run_id}-draft-results.jsonl"
    write_jsonl(
        draft_results,
        [
            {
                "post_id": draft_task["post_id"],
                "evidence_hash": draft_task["evidence_hash"],
                "analysis_hash": draft_task["analysis_hash"],
                "draft_text": (
                    "AI review: {PUBLIC_RATING}. The explanation is useful; "
                    "adding one concrete source would make it stronger."
                ),
            }
        ],
    )
    assert import_draft_results(
        conn,
        run_id,
        draft_results,
        actor="codex-drafter",
    ) == {"applied": 1}

    review_queue = tmp_path / f"{run_id}-review.jsonl"
    assert export_review_queue(conn, run_id, review_queue) == 1
    review_task = read_jsonl(review_queue)[0]
    review_results = tmp_path / f"{run_id}-review-results.jsonl"
    review_record = {
        "post_id": review_task["post_id"],
        "evidence_hash": review_task["evidence_hash"],
        "analysis_hash": review_task["analysis_hash"],
        "draft_hash": review_task["draft_hash"],
        "target_url": review_task["target_url"],
        "content_key": review_task["content_key"],
        "expected_account": review_task["expected_account"],
        "decision_hash": review_task["decision_hash"],
        "analysis_result_hash": review_task["analysis_result_hash"],
        "independent_review": True,
        "approved": True,
        "grounding": "pass",
        "usefulness": "pass",
        "tone": "pass",
        "language": "pass",
        "ai_disclosure": "pass",
        "rating_consistency": "pass",
        "positive_only": "pass",
    }
    write_jsonl(review_results, [review_record])

    with pytest.raises(StageGateError, match="cannot approve its own"):
        import_review_results(
            conn,
            run_id,
            review_results,
            actor="codex-drafter",
        )

    assert import_review_results(
        conn,
        run_id,
        review_results,
        actor="codex-reviewer",
    ) == {"applied": 1, "rejected": 0}
    return run_id


def test_preflight_runs_before_collector_and_exact_count_deduplicates(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn, requested=2)
        calls = []
        collector = RecordingCollector(
            calls,
            [
                evidence("1"),
                evidence("1"),
                evidence("bad", ready=False),
                evidence("2"),
                evidence("3"),
            ],
        )

        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(calls),
                collector=collector,
            )
        )

        assert calls == ["preflight", "collector"]
        assert collector.arguments == {
            "topic": "coffee",
            "requested_count": 2,
            "max_comments": 20,
            "max_pages": 10,
        }
        assert result["status"] == "collection_complete"
        assert result["unique_collected"] == 3
        assert result["evidence_ready"] == 2
        assert result["expected_account"] == "creator"
        assert (
            result["stage_telemetry"]["browser_preflight"]["recorded_operation_ms"] >= 0
        )
        assert result["stage_telemetry"]["collection"]["recorded_operation_ms"] >= 0
        browser_audit = json.loads(result["browser_preflight_json"])
        assert browser_audit["observed_account"] == "creator"
        assert "cdp_url" not in browser_audit
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM engage_tiktok_posts WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 3
        )
        assert (
            [
                row[0]
                for row in conn.execute(
                    """
                SELECT post_id FROM engage_tiktok_posts
                WHERE run_id=? AND evidence_ready=1 ORDER BY post_id
                """,
                    (run_id,),
                )
            ]
            == ["1", "2"]
        )
    finally:
        conn.close()


def test_ai_exports_use_compact_hash_bound_evidence_without_mutating_raw_db(
    tmp_path,
):
    record = evidence("compact-1")
    record["comments"] = [
        {
            "cid": "comment-1",
            "text": "A useful top-level point",
            "digg_count": 12,
            "create_time": 123,
            "share_info": {"url": "https://signed.example/comment"},
            "user": {
                "unique_id": "reader",
                "nickname": "Reader",
                "avatar_thumb": {
                    "url_list": ["https://signed.example/avatar?token=secret"]
                },
                "sec_uid": "large-transport-identity",
            },
            "reply_comment": [
                {
                    "cid": "reply-1",
                    "reply_id": "comment-1",
                    "text": "A relevant reply",
                    "digg_count": 3,
                    "user": {
                        "unique_id": "second_reader",
                        "nickname": "Second Reader",
                    },
                }
            ],
        }
    ]
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn)
        calls = []
        asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(calls),
                collector=RecordingCollector(calls, [record]),
            )
        )
        stored = conn.execute(
            """
            SELECT evidence_json, evidence_hash
            FROM engage_tiktok_posts
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        raw_packet = json.loads(stored["evidence_json"])
        assert "avatar_thumb" in raw_packet["comments"][0]["user"]

        queue = tmp_path / "analysis.jsonl"
        assert export_analysis_queue(conn, run_id, queue) == 1
        task = read_jsonl(queue)[0]
        projection = task["evidence_packet"]
        serialized_projection = json.dumps(projection)

        assert projection == compact_ai_evidence_projection(
            raw_packet,
            evidence_hash=stored["evidence_hash"],
        )
        assert projection["source_evidence_hash"] == stored["evidence_hash"]
        assert task["evidence_projection_hash"] == engage_tiktok.json_hash(projection)
        assert projection["comments"][0]["text"] == "A useful top-level point"
        assert projection["comments"][0]["replies"][0]["text"] == ("A relevant reply")
        assert "avatar_thumb" not in serialized_projection
        assert "signed.example" not in serialized_projection
        assert "large-transport-identity" not in serialized_projection
    finally:
        conn.close()


def test_generated_publisher_command_uses_required_python311(tmp_path):
    command = publisher_dry_run_command(
        tmp_path / "state.sqlite",
        "publication-1",
    )

    assert command[0] == (
        r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
    )
    assert command[1] == "publish_pending.py"
    assert command[-2:] == ["--publication-id", "publication-1"]
    assert command[:2] != ["py", "-3"]


def test_resume_collect_requires_an_explicit_database_and_run_id(tmp_path):
    args = engage_tiktok.parse_args(
        [
            "--database",
            str(tmp_path / "state.sqlite"),
            "resume-collect",
            "--run-id",
            "run-partial",
        ]
    )

    assert args.command == "resume-collect"
    assert args.run_id == "run-partial"
    assert args.database == tmp_path / "state.sqlite"


def test_refresh_selection_and_staleness_are_immutable_across_resume(
    tmp_path,
):
    class PolicyAwareCollector:
        def __init__(self, calls, records, *, error=None):
            self.calls = calls
            self.records = records
            self.error = error
            self.saved_policy = ""
            self.saved_candidates = []

        async def collect(
            self,
            *,
            topic,
            requested_count,
            max_comments,
            max_pages,
            record_callback=None,
            existing_post_ids=(),
            initial_evidence_ready_count=0,
            collection_policy="new_only",
            global_known_post_ids=(),
            current_run_post_ids=(),
            refresh_candidates=(),
            candidate_reserver=None,
        ):
            self.calls.append("collector")
            self.saved_policy = collection_policy
            self.saved_candidates = list(refresh_candidates)
            for record in self.records:
                record_callback(record)
            if self.error:
                raise self.error
            return list(self.records)

    stale_before = "2026-07-28T12:00:00+07:00"
    selected = [
        {
            "post_id": post_id,
            "canonical_url": (f"https://www.tiktok.com/@creator/video/{post_id}"),
            "creator": "creator",
            "caption": f"Cached {post_id}",
        }
        for post_id in ("41", "42")
    ]
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = create_run(
            conn,
            project="refresh-test",
            topic="coffee",
            requested_count=2,
            max_comments=20,
            max_pages=10,
            collection_policy="refresh_known",
            music_catalogs=(),
            refresh_candidates=selected,
            refresh_stale_before=stale_before,
            run_id="refresh-run",
        )
        first_calls = []
        first = PolicyAwareCollector(
            first_calls,
            [evidence("41")],
            error=RuntimeError("interrupt refresh"),
        )
        with pytest.raises(RuntimeError, match="interrupt refresh"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(first_calls),
                    collector=first,
                )
            )

        resume_args = parse_args(
            [
                "--database",
                str(tmp_path / "state.sqlite"),
                "resume-collect",
                "--run-id",
                run_id,
            ]
        )
        assert not hasattr(resume_args, "collection_policy")
        assert not hasattr(resume_args, "refresh_stale_before")

        resumed_calls = []
        resumed = PolicyAwareCollector(
            resumed_calls,
            [evidence("42")],
        )
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(resumed_calls),
                collector=resumed,
                resume=True,
            )
        )

        saved = conn.execute(
            """
            SELECT collection_policy, refresh_post_ids_json,
                   refresh_candidates_json, refresh_stale_before
            FROM engage_tiktok_runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        assert saved["collection_policy"] == "refresh_known"
        assert json.loads(saved["refresh_post_ids_json"]) == ["41", "42"]
        assert saved["refresh_stale_before"] == stale_before
        assert resumed.saved_policy == "refresh_known"
        assert [row["id"] for row in resumed.saved_candidates] == [
            "41",
            "42",
        ]
        assert result["status"] == "collection_complete"
    finally:
        conn.close()


def test_cli_explicit_refresh_ids_bypass_automatic_stale_cutoff(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "project" / "engage_state.sqlite"
    master_database = tmp_path / "master" / "tiktok_master.sqlite"
    selection_calls = []

    def fake_select(
        conn,
        schema,
        *,
        topic,
        post_ids,
        stale_before,
    ):
        selection_calls.append(
            {
                "schema": schema,
                "topic": topic,
                "post_ids": list(post_ids),
                "stale_before": stale_before,
            }
        )
        return [
            {
                "post_id": post_id,
                "canonical_url": (f"https://www.tiktok.com/@creator/video/{post_id}"),
                "creator_handle": "creator",
                "latest_evidence_json": json.dumps(
                    {
                        "post_id": post_id,
                        "url": ("https://www.tiktok.com/@creator/video/" f"{post_id}"),
                        "creator": "creator",
                        "caption": f"Cached {post_id}",
                        "metrics": {"views": 1},
                    }
                ),
            }
            for post_id in post_ids
        ]

    async def fake_collect_exact(conn, *, run_id, **kwargs):
        return {"status": "collection_not_executed", "run_id": run_id}

    monkeypatch.setattr(
        engage_tiktok,
        "select_refresh_candidates",
        fake_select,
    )
    monkeypatch.setattr(
        engage_tiktok,
        "collect_exact",
        fake_collect_exact,
    )

    assert (
        engage_tiktok.main(
            [
                "--database",
                str(database),
                "--master-database",
                str(master_database),
                "collect",
                "--project",
                "explicit-refresh",
                "--topic",
                "coffee",
                "--posts",
                "2",
                "--collection-policy",
                "refresh_known",
                "--refresh-post-id",
                "81",
                "--refresh-post-id",
                "82",
            ]
        )
        == 0
    )

    assert selection_calls == [
        {
            "schema": "master",
            "topic": "coffee",
            "post_ids": ["81", "82"],
            "stale_before": "",
        }
    ]
    conn = connect_database(database)
    try:
        saved = conn.execute(
            """
            SELECT collection_policy, refresh_post_ids_json,
                   refresh_stale_before, master_database
            FROM engage_tiktok_runs
            """
        ).fetchone()
        assert saved["collection_policy"] == "refresh_known"
        assert json.loads(saved["refresh_post_ids_json"]) == ["81", "82"]
        assert saved["refresh_stale_before"] == ""
        assert Path(saved["master_database"]) == master_database.resolve()
    finally:
        conn.close()


def test_same_file_master_schema_enforces_collection_and_stores_snapshot(
    tmp_path,
):
    database = tmp_path / "same-file.sqlite"

    class LeaseAwareCollector:
        async def collect(
            self,
            *,
            topic,
            requested_count,
            max_comments,
            max_pages,
            record_callback=None,
            existing_post_ids=(),
            initial_evidence_ready_count=0,
            collection_policy="new_only",
            global_known_post_ids=(),
            current_run_post_ids=(),
            refresh_candidates=(),
            candidate_reserver=None,
        ):
            record = evidence("same-file-1")
            assert candidate_reserver is not None
            assert candidate_reserver("same-file-1", record)
            assert record_callback(record) is True
            return [record]

    conn = connect_database(
        database,
        master_database=database,
    )
    try:
        assert engage_tiktok._master_database_schema(conn) == "main"
        run_id = create_run(
            conn,
            project="same-file-master",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=3,
            music_catalogs=(),
            master_database=str(database.resolve()),
            run_id="same-file-run",
        )
        calls = []
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(calls),
                collector=LeaseAwareCollector(),
            )
        )

        assert result["status"] == "collection_complete"
        assert (
            conn.execute("SELECT COUNT(*) FROM tiktok_master_snapshots").fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tiktok_master_collection_leases"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_checkpoint_master_guard_rejection_continues_to_replacement(
    tmp_path,
    monkeypatch,
):
    local_database = tmp_path / "project.sqlite"
    master_database = tmp_path / "master.sqlite"
    real_guard = engage_tiktok.collection_candidate_guard

    def reject_first_candidate(conn, schema, **kwargs):
        if kwargs["post_id"] == "101":
            return {
                "allowed": False,
                "reason": "new_only_post_became_known",
                "post_id": "101",
            }
        return real_guard(conn, schema, **kwargs)

    monkeypatch.setattr(
        engage_tiktok,
        "collection_candidate_guard",
        reject_first_candidate,
    )

    class ReplacementCollector:
        async def collect(
            self,
            *,
            topic,
            requested_count,
            max_comments,
            max_pages,
            record_callback=None,
            existing_post_ids=(),
            initial_evidence_ready_count=0,
            collection_policy="new_only",
            global_known_post_ids=(),
            current_run_post_ids=(),
            refresh_candidates=(),
            candidate_reserver=None,
        ):
            returned = []
            for post_id in ("101", "102"):
                record = evidence(post_id)
                assert candidate_reserver(post_id, record)
                returned.append(record)
                if record_callback(record):
                    break
            return returned

    conn = connect_database(
        local_database,
        master_database=master_database,
    )
    try:
        run_id = create_run(
            conn,
            project="checkpoint-guard",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=3,
            music_catalogs=(),
            master_database=str(master_database.resolve()),
            run_id="checkpoint-guard-run",
        )
        calls = []
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(calls),
                collector=ReplacementCollector(),
            )
        )

        assert result["status"] == "collection_complete"
        assert (
            [
                row[0]
                for row in conn.execute(
                    """
                SELECT post_id
                FROM engage_tiktok_posts
                WHERE run_id=? AND evidence_ready=1
                """,
                    (run_id,),
                ).fetchall()
            ]
            == ["102"]
        )
        rejected = conn.execute(
            """
            SELECT payload_json
            FROM engage_tiktok_events
            WHERE run_id=? AND post_id='101'
              AND event='candidate_rejected'
            """,
            (run_id,),
        ).fetchone()
        assert (
            json.loads(rejected[0])["master_guard"]["reason"]
            == "new_only_post_became_known"
        )
    finally:
        conn.close()


def test_blank_legacy_run_master_path_is_bound_once(tmp_path):
    database = tmp_path / "legacy-run.sqlite"
    first_master = tmp_path / "master-one.sqlite"
    second_master = tmp_path / "master-two.sqlite"
    conn = connect_database(database)
    try:
        run_id = create_run(
            conn,
            project="legacy-master-binding",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=3,
            master_database="",
            run_id="legacy-blank-master",
        )

        bound = engage_tiktok._bind_run_master_database(
            conn,
            run_id,
            first_master,
        )
        assert Path(bound) == first_master.resolve()
        assert (
            Path(
                conn.execute(
                    """
                SELECT master_database
                FROM engage_tiktok_runs
                WHERE run_id=?
                """,
                    (run_id,),
                ).fetchone()[0]
            )
            == first_master.resolve()
        )

        with pytest.raises(StageGateError, match="immutable master-database"):
            engage_tiktok._bind_run_master_database(
                conn,
                run_id,
                second_master,
            )
    finally:
        conn.close()


def test_project_connection_binds_all_blank_runs_and_rejects_registry_switch(
    tmp_path,
):
    database = tmp_path / "legacy-project.sqlite"
    first_master = tmp_path / "master-one.sqlite"
    second_master = tmp_path / "master-two.sqlite"
    conn = connect_database(database)
    try:
        create_run(
            conn,
            project="legacy-project-binding",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=3,
            master_database="",
            run_id="legacy-project-run",
        )
    finally:
        conn.close()

    conn = connect_database(database, master_database=first_master)
    try:
        stored = conn.execute(
            """
            SELECT master_database
            FROM engage_tiktok_runs
            WHERE run_id='legacy-project-run'
            """
        ).fetchone()[0]
        assert Path(stored) == first_master.resolve()
    finally:
        conn.close()

    with pytest.raises(StageGateError, match="different immutable"):
        connect_database(database, master_database=second_master)
    assert not second_master.exists()


def test_listen_workflow_stops_after_collection_and_cannot_export_analysis(
    tmp_path,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = create_run(
            conn,
            project="listen-test",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=10,
            workflow="listen",
            music_catalogs=(),
            run_id="listen-run",
        )
        calls = []
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(calls),
                collector=RecordingCollector(calls, [evidence("51")]),
            )
        )

        assert result["status"] == "collection_complete"
        assert result["workflow"] == "listen"
        with pytest.raises(
            StageGateError,
            match="LISTEN workflow is collection-only",
        ):
            export_analysis_queue(
                conn,
                run_id,
                tmp_path / "analysis.jsonl",
            )
    finally:
        conn.close()


def test_refresh_known_checkpoint_appends_master_snapshot(tmp_path):
    local_database = tmp_path / "project" / "engage_state.sqlite"
    master_database = tmp_path / "master" / "tiktok_master.sqlite"
    conn = connect_database(
        local_database,
        master_database=master_database,
    )
    try:
        seed_run = create_run(
            conn,
            project="master-refresh",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=10,
            music_catalogs=(),
            run_id="seed-run",
        )
        seed_calls = []
        asyncio.run(
            collect_exact(
                conn,
                run_id=seed_run,
                preflight=RecordingPreflight(seed_calls),
                collector=CheckpointingCollector(
                    seed_calls,
                    [evidence("71")],
                ),
            )
        )
        before = engage_tiktok.master_summary(conn, "master")
        selected = engage_tiktok.select_refresh_candidates(
            conn,
            "master",
            topic="coffee",
            post_ids=("71",),
            stale_before="2099-01-01T00:00:00+00:00",
        )
        assert [candidate["post_id"] for candidate in selected] == ["71"]

        refresh_run = create_run(
            conn,
            project="master-refresh",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=10,
            collection_policy="refresh_known",
            music_catalogs=(),
            refresh_candidates=selected,
            refresh_stale_before="2099-01-01T00:00:00+00:00",
            master_database=str(master_database.resolve()),
            run_id="refresh-run",
        )

        class SnapshotRefreshCollector:
            async def collect(
                self,
                *,
                topic,
                requested_count,
                max_comments,
                max_pages,
                record_callback=None,
                existing_post_ids=(),
                initial_evidence_ready_count=0,
                collection_policy="new_only",
                global_known_post_ids=(),
                current_run_post_ids=(),
                refresh_candidates=(),
                candidate_reserver=None,
            ):
                assert collection_policy == "refresh_known"
                assert [row["id"] for row in refresh_candidates] == ["71"]
                assert candidate_reserver("71", refresh_candidates[0])
                refreshed = {
                    **evidence("71"),
                    "caption": "Fresh changed caption",
                    "view_count": 999,
                    "discovery_method": "master_registry_refresh",
                    "metadata_refresh_ok": True,
                }
                assert record_callback(refreshed) is True
                return [refreshed]

        refresh_calls = []
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=refresh_run,
                preflight=RecordingPreflight(refresh_calls),
                collector=SnapshotRefreshCollector(),
            )
        )
        after = engage_tiktok.master_summary(conn, "master")

        assert result["status"] == "collection_complete"
        assert before["snapshots"] == 1
        assert after["snapshots"] == 2
        assert (
            conn.execute(
                """
            SELECT COUNT(*)
            FROM master.tiktok_master_snapshots
            WHERE post_id='71'
            """
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_master_snapshot_failure_rolls_back_local_checkpoint(
    tmp_path,
    monkeypatch,
):
    conn = connect_database(
        tmp_path / "project" / "engage_state.sqlite",
        master_database=tmp_path / "master" / "tiktok_master.sqlite",
    )
    try:
        run_id = create_run(
            conn,
            project="atomic-master",
            topic="coffee",
            requested_count=1,
            max_comments=20,
            max_pages=10,
            music_catalogs=(),
            run_id="atomic-master-run",
        )

        def fail_snapshot(*args, **kwargs):
            raise RuntimeError("master snapshot failure")

        monkeypatch.setattr(
            engage_tiktok,
            "record_evidence_snapshot",
            fail_snapshot,
        )
        calls = []
        with pytest.raises(RuntimeError, match="master snapshot failure"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(calls),
                    collector=CheckpointingCollector(
                        calls,
                        [evidence("91")],
                    ),
                )
            )

        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM engage_tiktok_posts
            WHERE run_id=?
            """,
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            engage_tiktok.master_summary(
                conn,
                "master",
            )["snapshots"]
            == 0
        )
        assert run_status(conn, run_id)["status"] == "collection_failed"
    finally:
        conn.close()


def test_master_full_sync_runs_once_at_terminal_not_per_checkpoint(
    tmp_path,
    monkeypatch,
):
    conn = connect_database(
        tmp_path / "project" / "engage_state.sqlite",
        master_database=tmp_path / "master" / "tiktok_master.sqlite",
    )
    try:
        run_id = create_run(
            conn,
            project="master-performance",
            topic="coffee",
            requested_count=3,
            max_comments=20,
            max_pages=10,
            music_catalogs=(),
            run_id="master-performance-run",
        )
        original_sync = engage_tiktok.sync_local_database
        sync_calls = []

        def recording_sync(*args, **kwargs):
            sync_calls.append((args, kwargs))
            return original_sync(*args, **kwargs)

        monkeypatch.setattr(
            engage_tiktok,
            "sync_local_database",
            recording_sync,
        )
        calls = []
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(calls),
                collector=CheckpointingCollector(
                    calls,
                    [evidence("101"), evidence("102"), evidence("103")],
                ),
            )
        )

        assert result["status"] == "collection_complete"
        assert len(sync_calls) == 1
    finally:
        conn.close()


def test_failed_preflight_never_calls_collector(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn)
        calls = []
        collector = RecordingCollector(calls, [evidence("1")])

        with pytest.raises(BrowserPreflightError, match="login required"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(calls, fail=True),
                    collector=collector,
                )
            )

        assert calls == ["preflight"]
        assert run_status(conn, run_id)["status"] == "browser_blocked"
        assert (
            conn.execute(
                """
            SELECT ready FROM engage_tiktok_browser_checks
            WHERE run_id=?
            """,
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_underfilled_collection_is_persisted_and_blocks_analysis(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn, requested=3)
        calls = []
        with pytest.raises(CollectionIncompleteError, match=r"2/3"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(calls),
                    collector=RecordingCollector(
                        calls,
                        [evidence("1"), evidence("2"), evidence("2")],
                    ),
                )
            )

        status = run_status(conn, run_id)
        assert status["status"] == "collection_incomplete"
        assert status["evidence_ready"] == 2
        with pytest.raises(StageGateError, match="exact-count gate failed"):
            export_analysis_queue(conn, run_id, tmp_path / "analysis.jsonl")
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='table' AND name IN ('analysis_runs', 'publication_queue')
            """
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_incremental_checkpoints_survive_failure_and_resume_to_exact_count(
    tmp_path,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn, requested=3)
        first_calls = []
        with pytest.raises(RuntimeError, match="simulated collector failure"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(first_calls),
                    collector=CheckpointingCollector(
                        first_calls,
                        [evidence("1"), evidence("2")],
                        error=RuntimeError("simulated collector failure"),
                    ),
                )
            )

        failed = run_status(conn, run_id)
        assert failed["status"] == "collection_failed"
        assert failed["evidence_ready"] == 2
        assert failed["collection_attempt_id"] == ""
        with pytest.raises(StageGateError, match="exact-count gate failed"):
            export_analysis_queue(conn, run_id, tmp_path / "analysis.jsonl")

        resume_calls = []
        resumed_collector = CheckpointingCollector(
            resume_calls,
            [evidence("3"), evidence("4")],
        )
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(resume_calls),
                collector=resumed_collector,
                resume=True,
            )
        )

        assert result["status"] == "collection_complete"
        assert result["evidence_ready"] == 3
        assert result["collection_attempt_id"] == ""
        assert resumed_collector.existing_post_ids == ("1", "2")
        assert resumed_collector.initial_evidence_ready_count == 2
        assert (
            [
                row[0]
                for row in conn.execute(
                    """
                SELECT post_id
                FROM engage_tiktok_posts
                WHERE run_id=? AND evidence_ready=1
                ORDER BY post_id
                """,
                    (run_id,),
                )
            ]
            == ["1", "2", "3"]
        )
    finally:
        conn.close()


def test_resume_repairs_partial_checkpoint_without_overwriting_ready_evidence(
    tmp_path,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn, requested=2)
        first_calls = []
        with pytest.raises(RuntimeError):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(first_calls),
                    collector=CheckpointingCollector(
                        first_calls,
                        [evidence("1"), evidence("repair", ready=False)],
                        error=RuntimeError("stop after partial checkpoint"),
                    ),
                )
            )
        original_ready_hash = conn.execute(
            """
            SELECT evidence_hash
            FROM engage_tiktok_posts
            WHERE run_id=? AND post_id='1'
            """,
            (run_id,),
        ).fetchone()[0]

        resume_calls = []
        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(resume_calls),
                collector=CheckpointingCollector(
                    resume_calls,
                    [evidence("1"), evidence("repair")],
                ),
                resume=True,
            )
        )

        assert result["evidence_ready"] == 2
        assert (
            conn.execute(
                """
            SELECT evidence_hash
            FROM engage_tiktok_posts
            WHERE run_id=? AND post_id='1'
            """,
                (run_id,),
            ).fetchone()[0]
            == original_ready_hash
        )
        repaired = conn.execute(
            """
            SELECT status, evidence_ready, collection_error
            FROM engage_tiktok_posts
            WHERE run_id=? AND post_id='repair'
            """,
            (run_id,),
        ).fetchone()
        assert tuple(repaired) == ("collected", 1, "")
    finally:
        conn.close()


def test_resume_recovers_exact_hard_crash_checkpoints_without_browser_access(
    tmp_path,
):
    class SimulatedProcessCrash(BaseException):
        pass

    class NeverPreflight:
        async def ensure_ready(self):
            raise AssertionError("exact checkpoint recovery must stay offline")

    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn, requested=2)
        calls = []
        with pytest.raises(SimulatedProcessCrash):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(calls),
                    collector=CheckpointingCollector(
                        calls,
                        [evidence("1"), evidence("2")],
                        error=SimulatedProcessCrash(),
                    ),
                )
            )

        crashed = run_status(conn, run_id)
        assert crashed["status"] == "collecting"
        assert crashed["evidence_ready"] == 2
        assert crashed["collection_attempt_id"]

        result = asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=NeverPreflight(),
                collector=CheckpointingCollector([], []),
                resume=True,
            )
        )
        assert result["status"] == "collection_complete"
        assert result["evidence_ready"] == 2
        assert result["collection_attempt_id"] == ""
    finally:
        conn.close()


def test_superseded_collection_attempt_is_fenced_from_writes(tmp_path):
    class SupersedingCollector:
        async def collect(
            self,
            *,
            topic,
            requested_count,
            max_comments,
            max_pages,
            record_callback=None,
            existing_post_ids=(),
            initial_evidence_ready_count=0,
        ):
            conn.execute(
                """
                UPDATE engage_tiktok_runs
                SET collection_attempt_id='newer-attempt'
                WHERE run_id=?
                """,
                (run_id,),
            )
            conn.commit()
            record_callback(evidence("1"))
            return []

    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn)
        calls = []
        with pytest.raises(StageGateError, match="superseded"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(calls),
                    collector=SupersedingCollector(),
                )
            )
        row = conn.execute(
            """
            SELECT status, collection_attempt_id
            FROM engage_tiktok_runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        assert tuple(row) == ("collecting", "newer-attempt")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM engage_tiktok_posts WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_offline_ai_checkpoints_do_not_revalidate_profile7(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "state.sqlite"
    conn = connect_database(database)
    try:
        run_id = new_run(conn)
        calls = []
        asyncio.run(
            collect_exact(
                conn,
                run_id=run_id,
                preflight=RecordingPreflight(calls),
                collector=RecordingCollector(calls, [evidence("1")]),
            )
        )
    finally:
        conn.close()

    async def unexpected_browser_revalidation(*args, **kwargs):
        raise AssertionError("offline AI checkpoints must not revalidate Profile 7")

    monkeypatch.setattr(
        engage_tiktok,
        "revalidate_run_browser",
        unexpected_browser_revalidation,
    )
    output = tmp_path / "analysis.jsonl"
    assert (
        engage_tiktok.main(
            [
                "--database",
                str(database),
                "export-analysis",
                "--run-id",
                run_id,
                "--file",
                str(output),
            ]
        )
        == 0
    )
    assert output.exists()
    assert BROWSER_REVALIDATION_COMMANDS == frozenset({"handoff"})


def test_collector_reported_zero_result_failure_is_collection_incomplete(
    tmp_path,
):
    class ZeroResultFailureCollector:
        last_diagnostics = {
            "collection_stop_reason": "discovery_error_before_collection",
            "attempted_candidates": 0,
        }

        async def collect(self, **kwargs):
            requested = kwargs["requested_count"]
            raise CollectionIncompleteError(
                f"collection_incomplete: 0/{requested} "
                "evidence-ready TikTok posts; "
                "reason=discovery_error_before_collection"
            )

    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn, requested=50)
        calls = []

        with pytest.raises(CollectionIncompleteError, match=r"0/50"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(calls),
                    collector=ZeroResultFailureCollector(),
                )
            )

        status = run_status(conn, run_id)
        assert status["status"] == "collection_incomplete"
        assert status["requested"] == 50
        assert status["unique_collected"] == 0
        assert status["evidence_ready"] == 0
        assert status["analyzed"] == 0
        with pytest.raises(StageGateError, match="exact-count gate failed"):
            export_analysis_queue(conn, run_id, tmp_path / "analysis.jsonl")
    finally:
        conn.close()


def test_twelve_of_fifty_never_advances_to_analysis(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = new_run(conn, requested=50)
        calls = []
        collector = RecordingCollector(
            calls,
            [evidence(str(index)) for index in range(1, 13)],
        )
        collector.last_diagnostics = {
            "collection_stop_reason": "query_frontier_exhausted",
        }

        with pytest.raises(CollectionIncompleteError, match=r"12/50"):
            asyncio.run(
                collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=RecordingPreflight(calls),
                    collector=collector,
                )
            )

        status = run_status(conn, run_id)
        assert status["status"] == "collection_incomplete"
        assert status["requested"] == 50
        assert status["unique_collected"] == 12
        assert status["evidence_ready"] == 12
        assert status["analyzed"] == 0
        assert status["drafted"] == 0
        assert status["reviewed"] == 0
        assert status["stored"] == 0
        analysis_path = tmp_path / "analysis.jsonl"
        with pytest.raises(StageGateError, match="exact-count gate failed"):
            export_analysis_queue(conn, run_id, analysis_path)
        assert not analysis_path.exists()
    finally:
        conn.close()


def test_live_sequence_requires_independent_review_and_exact_authorization_hashes(
    tmp_path,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = advance_to_reviewed(conn, tmp_path, mode="live")
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()

        assert post["status"] == "reviewed"
        assert post["post_quality_score"] == 80
        assert post["conversation_value_score"] == 100
        assert post["analysis_score"] == 84
        assert "8.4/10" in post["draft_text"]
        assert "{PUBLIC_RATING}" not in post["draft_text"]
        assert post["draft_hash"]
        assert post["review_hash"]

        ai_presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        with pytest.raises(StageGateError, match="non-AI user authorization"):
            authorize_response(
                conn,
                run_id,
                post["post_id"],
                authorized_by="Codex",
                expected_draft_hash=post["draft_hash"],
                expected_review_hash=post["review_hash"],
                expected_presentation_hash=ai_presentation["presentation_hash"],
                approval_token=ai_presentation["approval_token"],
            )
        reviewer_presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        with pytest.raises(StageGateError, match="non-AI user authorization"):
            authorize_response(
                conn,
                run_id,
                post["post_id"],
                authorized_by="codex-reviewer",
                expected_draft_hash=post["draft_hash"],
                expected_review_hash=post["review_hash"],
                expected_presentation_hash=reviewer_presentation["presentation_hash"],
                approval_token=reviewer_presentation["approval_token"],
            )
        wrong_hash_presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        with pytest.raises(StageGateError, match="exact response text"):
            authorize_response(
                conn,
                run_id,
                post["post_id"],
                authorized_by="user@example",
                expected_draft_hash="wrong",
                expected_review_hash=post["review_hash"],
                expected_presentation_hash=wrong_hash_presentation["presentation_hash"],
                approval_token=wrong_hash_presentation["approval_token"],
            )

        wrong_token_presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        with pytest.raises(StageGateError, match="approval token"):
            authorize_response(
                conn,
                run_id,
                post["post_id"],
                authorized_by="user@example",
                expected_draft_hash=post["draft_hash"],
                expected_review_hash=post["review_hash"],
                expected_presentation_hash=wrong_token_presentation[
                    "presentation_hash"
                ],
                approval_token="wrong-token",
            )

        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        authorization = authorize_response(
            conn,
            run_id,
            post["post_id"],
            authorized_by="user@example",
            expected_draft_hash=post["draft_hash"],
            expected_review_hash=post["review_hash"],
            expected_presentation_hash=presentation["presentation_hash"],
            approval_token=presentation["approval_token"],
        )
        handoff = handoff_publication(conn, run_id, post["post_id"])

        assert handoff["publication_id"] == authorization["publication_id"]
        assert handoff["draft_hash"] == post["draft_hash"]
        assert handoff["review_hash"] == post["review_hash"]
        assert handoff["presentation_hash"] == presentation["presentation_hash"]
        assert handoff["handoff_hash"]
        assert conn.execute("SELECT COUNT(*) FROM publication_queue").fetchone()[0] == 1
        queued = conn.execute("SELECT * FROM publication_queue").fetchone()
        decision = json.loads(queued["decision_json"])
        assert queued["status"] == "approved"
        assert queued["mode"] == "live"
        assert queued["draft_hash"] == post["draft_hash"]
        assert queued["ai_review_status"] == "approved"
        assert queued["ai_review_hash"] == post["review_hash"]
        assert (
            queued["authorization_presentation_hash"]
            == presentation["presentation_hash"]
        )
        assert queued["authorization_text_hash"] == post["draft_hash"]
        assert queued["authorization_review_hash"] == post["review_hash"]
        assert decision["score"] == 84
        assert decision["post_score"] == 80
        assert decision["conversation_score"] == 100
        assert decision["public_rating"]["formatted"] == "8.4/10"

        adapter_record = load_approved_publication(
            tmp_path / "state.sqlite",
            handoff["publication_id"],
        )
        assert adapter_record["final_text"] == post["draft_text"]
        assert adapter_record["final_text_hash"] == post["draft_hash"]
        assert adapter_record["ai_review_hash"] == post["review_hash"]
        assert (
            adapter_record["authorization_presentation_hash"]
            == presentation["presentation_hash"]
        )
    finally:
        conn.close()


def test_shadow_response_cannot_be_live_authorized(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = advance_to_reviewed(
            conn,
            tmp_path,
            mode="shadow",
            run_id="shadow-run",
        )
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()

        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        with pytest.raises(StageGateError, match="SHADOW"):
            authorize_response(
                conn,
                run_id,
                post["post_id"],
                authorized_by="user@example",
                expected_draft_hash=post["draft_hash"],
                expected_review_hash=post["review_hash"],
                expected_presentation_hash=presentation["presentation_hash"],
                approval_token=presentation["approval_token"],
            )
        with pytest.raises(StageGateError, match="live authorization"):
            handoff_publication(conn, run_id, post["post_id"])
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='table' AND name='publication_queue'
            """
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_authorization_rejects_wrong_presentation_token(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = advance_to_reviewed(
            conn,
            tmp_path,
            mode="live",
            run_id="wrong-token-run",
        )
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )

        with pytest.raises(StageGateError, match="approval token"):
            authorize_response(
                conn,
                run_id,
                post["post_id"],
                authorized_by="user@example",
                expected_draft_hash=post["draft_hash"],
                expected_review_hash=post["review_hash"],
                expected_presentation_hash=presentation["presentation_hash"],
                approval_token="not-the-presented-token",
            )

        assert (
            conn.execute(
                """
            SELECT status FROM engage_tiktok_posts
            WHERE run_id=? AND post_id=?
            """,
                (run_id, post["post_id"]),
            ).fetchone()[0]
            == "reviewed"
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("post_score", "conversation_score", "overall", "rating"),
    [
        (80, 100, 84, "8.4/10"),
        (100, 0, 90, "9/10"),
        (82, 82, 82, "8.2/10"),
        (83, 83, 83, "8.3/10"),
        (82.5, 82.5, 82.5, "8.3/10"),
    ],
)
def test_deterministic_overall_score_and_rating(
    post_score,
    conversation_score,
    overall,
    rating,
):
    score, diagnostics = canonical_analysis_score(post_score, conversation_score)
    assert score == overall
    assert diagnostics["overall_score"] == overall
    assert deterministic_public_rating(score)[1] == rating


def test_production_collector_closes_only_temporary_page(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"cdp_url": "ws://127.0.0.1:9223/devtools/browser/profile7"}),
        encoding="utf-8",
    )

    import social_browser

    designation = {
        "user_data_dir": str(tmp_path / "Edge User Data"),
        "profile_directory": "Profile 7",
        "designation_id": "profile7-designation",
        "mode": "existing_profile_attach",
    }
    monkeypatch.setattr(
        social_browser,
        "load_engage_profile7_designation",
        lambda runtime_dir: designation,
    )
    monkeypatch.setattr(
        social_browser,
        "load_verified_profile7_state",
        lambda runtime_dir, received_designation: {
            "cdp_url": "ws://127.0.0.1:9223/devtools/browser/profile7",
        },
    )

    async def fake_verified_context(received_browser, received_designation):
        assert received_designation == designation
        return received_browser.contexts[0], {"verified": True}

    async def fake_authentication(received_context, platform):
        assert received_context is browser.contexts[0]
        assert platform == "tiktok"
        return {"authenticated": True}

    monkeypatch.setattr(
        social_browser,
        "verified_profile_context",
        fake_verified_context,
    )
    monkeypatch.setattr(
        social_browser,
        "platform_authentication",
        fake_authentication,
    )

    class FakePage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

        async def goto(self, *_args, **_kwargs):
            return None

    page = FakePage()

    class FakeContext:
        async def cookies(self, _url):
            return [{"name": "sessionid", "value": "not-exposed"}]

        async def new_page(self):
            return page

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    browser = FakeBrowser()

    class FakeChromium:
        async def connect_over_cdp(self, cdp_url):
            assert cdp_url == ("ws://127.0.0.1:9223/devtools/browser/profile7")
            return browser

    class FakePlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeIntegration:
        def __init__(self, *, enable_api, persist_session_secrets):
            assert enable_api is True
            assert persist_session_secrets is False

        async def initialize_api(self, received_page):
            assert received_page is page
            return True

        async def discover_search_videos(
            self,
            received_page,
            topic,
            *,
            max_offsets,
            include_related_queries,
            target_count,
        ):
            assert received_page is page
            assert topic == "coffee"
            assert max_offsets == 3
            assert include_related_queries is False
            assert target_count == 2
            return [{**evidence("123"), "caption": "Useful coffee post"}]

        async def hydrate_video_candidates(self, received_page, candidates):
            assert received_page is page
            assert len(candidates) == 1

        async def get_comments_for_multiple_videos(
            self,
            received_page,
            candidates,
            *,
            max_comments,
            concurrency,
            use_checkpoints,
        ):
            assert received_page is page
            assert max_comments == 10
            assert concurrency == 2
            assert use_checkpoints is False
            return {
                "123": {
                    "comments": [],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "has_more": False,
                    "transcript_status": "unavailable",
                }
            }

    import playwright.async_api
    import tiktok_scraper.api_integration

    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: FakePlaywrightContext(),
    )
    monkeypatch.setattr(
        tiktok_scraper.api_integration,
        "TikTokAPIIntegration",
        FakeIntegration,
    )

    records = asyncio.run(
        TikTokBrowserCollector(state_path=state_path, concurrency=2).collect(
            topic="coffee",
            requested_count=1,
            max_comments=10,
            max_pages=3,
        )
    )

    assert len(records) == 1
    assert page.closed is True
    assert browser.close_calls == 0


def run_fake_production_collector(
    tmp_path,
    monkeypatch,
    integration_class,
    *,
    requested_count,
    max_pages=3,
    collector_options=None,
):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"cdp_url": ("ws://127.0.0.1:9223/devtools/browser/profile7")}),
        encoding="utf-8",
    )

    import playwright.async_api
    import social_browser
    import tiktok_scraper.api_integration

    designation = {
        "user_data_dir": str(tmp_path / "Edge User Data"),
        "profile_directory": "Profile 7",
        "designation_id": "profile7-designation",
        "mode": "existing_profile_attach",
    }
    monkeypatch.setattr(
        social_browser,
        "load_engage_profile7_designation",
        lambda runtime_dir: designation,
    )
    monkeypatch.setattr(
        social_browser,
        "load_verified_profile7_state",
        lambda runtime_dir, received_designation: {
            "cdp_url": ("ws://127.0.0.1:9223/devtools/browser/profile7"),
        },
    )

    class FakePage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

        async def goto(self, *_args, **_kwargs):
            return None

    page = FakePage()

    class FakeContext:
        async def new_page(self):
            return page

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    browser = FakeBrowser()

    async def fake_verified_context(
        received_browser,
        received_designation,
    ):
        assert received_designation == designation
        return received_browser.contexts[0], {"verified": True}

    async def fake_authentication(received_context, platform):
        assert received_context is browser.contexts[0]
        assert platform == "tiktok"
        return {"authenticated": True}

    monkeypatch.setattr(
        social_browser,
        "verified_profile_context",
        fake_verified_context,
    )
    monkeypatch.setattr(
        social_browser,
        "platform_authentication",
        fake_authentication,
    )

    class FakeChromium:
        async def connect_over_cdp(self, cdp_url):
            assert cdp_url == ("ws://127.0.0.1:9223/devtools/browser/profile7")
            return browser

    class FakePlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: FakePlaywrightContext(),
    )
    monkeypatch.setattr(
        tiktok_scraper.api_integration,
        "TikTokAPIIntegration",
        integration_class,
    )

    collector = TikTokBrowserCollector(
        state_path=state_path,
        concurrency=3,
    )
    records = asyncio.run(
        collector.collect(
            topic="coffee",
            requested_count=requested_count,
            max_comments=10,
            max_pages=max_pages,
            **dict(collector_options or {}),
        )
    )
    return records, collector, page, browser


def test_production_collector_uses_creator_inventory_as_all_target(
    tmp_path,
    monkeypatch,
):
    class FakeIntegration:
        def __init__(self, *, enable_api, persist_session_secrets):
            assert enable_api is True
            assert persist_session_secrets is False
            self.last_creator_profile_diagnostics = {}

        async def initialize_api(self, page):
            return True

        async def discover_creator_profile_posts(
            self,
            page,
            creator,
            *,
            collect_all,
            max_pages,
        ):
            assert creator == "maker"
            assert collect_all is True
            assert max_pages == 3
            self.last_creator_profile_diagnostics = {
                "terminal_verified": True,
                "inventory_complete": True,
                "candidate_count": 2,
                "bound_creator_identity": {
                    "id": "maker-stable-id",
                    "sec_uid": "maker-stable-secuid",
                },
                "stop_reason": "source_exhausted",
            }
            first = evidence("710")
            first.update(
                {
                    "username": "maker",
                    "url": "https://www.tiktok.com/@maker/video/710",
                }
            )
            second = evidence("711")
            second.update(
                {
                    "username": "maker",
                    "url": "https://www.tiktok.com/@maker/photo/711",
                    "content_type": "photo",
                    "visual_evidence_status": "unavailable",
                    "visual_evidence_terminal": True,
                    "visual_slide_count": 1,
                }
            )
            return [first, second]

        async def hydrate_video_candidates(self, page, candidates):
            return candidates

        async def get_comments_for_multiple_videos(
            self,
            page,
            candidates,
            *,
            max_comments,
            concurrency,
            use_checkpoints,
        ):
            assert max_comments == 10
            assert concurrency == 3
            assert use_checkpoints is False
            return {
                candidate["id"]: {
                    "comments": candidate["comments"],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "limit_reached": False,
                    "has_more": False,
                    "transcript": "",
                    "transcript_status": "unavailable",
                    "subtitle_no_caption_reason": "not_provided",
                    "source": "creator-profile-test",
                }
                for candidate in candidates
            }

    inventory_checkpoints = []
    evidence_checkpoints = []

    def checkpoint_inventory(candidates, metadata):
        inventory_checkpoints.append((list(candidates), dict(metadata)))
        assert metadata["terminal"] is True
        assert metadata["selected_post_ids"] == ["710", "711"]
        return 2

    def checkpoint_evidence(record):
        evidence_checkpoints.append(record)
        checkpoint_evidence.last_ready_accepted = True
        return len(evidence_checkpoints) == 2

    checkpoint_evidence.last_ready_accepted = False

    records, collector, page, browser = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=0,
        collector_options={
            "source_mode": "creator",
            "creator_handle": "maker",
            "creator_inventory_callback": checkpoint_inventory,
            "record_callback": checkpoint_evidence,
        },
    )

    assert [record["id"] for record in records] == ["710", "711"]
    assert [record["id"] for record in evidence_checkpoints] == ["710", "711"]
    assert len(inventory_checkpoints) == 1
    assert collector.last_diagnostics["collection_stop_reason"] == (
        "exact_count_reached"
    )
    assert collector.last_diagnostics["profile_inventory_count"] == 2
    assert page.closed is True
    assert browser.close_calls == 0


def test_production_collector_reuses_terminal_creator_inventory_proof(
    tmp_path,
    monkeypatch,
):
    first = evidence("710")
    first.update(
        {
            "username": "maker",
            "url": "https://www.tiktok.com/@maker/video/710",
        }
    )
    second = evidence("711")
    second.update(
        {
            "username": "maker",
            "url": "https://www.tiktok.com/@maker/video/711",
        }
    )
    inventory = [first, second]

    class FakeIntegration:
        def __init__(self, *, enable_api, persist_session_secrets):
            assert enable_api is True
            assert persist_session_secrets is False

        async def initialize_api(self, page):
            return True

        async def discover_creator_profile_posts(self, *_args, **_kwargs):
            raise AssertionError("frozen terminal inventory must be reused")

        async def hydrate_video_candidates(self, page, candidates):
            return candidates

        async def get_comments_for_multiple_videos(
            self,
            page,
            candidates,
            *,
            max_comments,
            concurrency,
            use_checkpoints,
        ):
            assert max_comments == 10
            assert concurrency == 3
            assert use_checkpoints is False
            return {
                candidate["id"]: {
                    "comments": candidate["comments"],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "limit_reached": False,
                    "has_more": False,
                    "transcript": "",
                    "transcript_status": "unavailable",
                    "subtitle_no_caption_reason": "not_provided",
                    "source": "creator-profile-resume-test",
                }
                for candidate in candidates
            }

    evidence_checkpoints = []

    def checkpoint_evidence(record):
        evidence_checkpoints.append(record)
        checkpoint_evidence.last_ready_accepted = True
        return len(evidence_checkpoints) == 2

    checkpoint_evidence.last_ready_accepted = False
    records, collector, page, browser = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=2,
        collector_options={
            "source_mode": "creator",
            "creator_handle": "maker",
            "creator_inventory": inventory,
            "creator_inventory_terminal": True,
            "creator_selected_post_ids": ["710", "711"],
            "record_callback": checkpoint_evidence,
        },
    )

    assert [record["id"] for record in records] == ["710", "711"]
    assert collector.last_diagnostics["terminal_verified"] is True
    assert collector.last_diagnostics["inventory_complete"] is True
    assert collector.last_diagnostics["has_more"] is False
    assert collector.last_diagnostics["source_exhausted"] is True
    assert collector.last_diagnostics["limit_reached"] is False
    assert collector.last_diagnostics["stop_reason"] == "source_exhausted"
    assert collector.last_diagnostics["unique_owner_posts_observed"] == 2
    assert page.closed is True
    assert browser.close_calls == 0


def test_production_collector_stops_at_exact_requested_ready_count(
    tmp_path,
    monkeypatch,
):
    class FakeIntegration:
        instance = None

        def __init__(self, *, enable_api, persist_session_secrets):
            self.hydrated_batches = []
            self.comment_batches = []
            self.last_search_diagnostics = {}
            type(self).instance = self

        async def initialize_api(self, page):
            return True

        async def discover_search_videos(
            self,
            page,
            topic,
            *,
            max_offsets,
            include_related_queries,
            target_count,
        ):
            assert target_count == 100
            assert include_related_queries is False
            self.last_search_diagnostics = {
                "candidate_target": target_count,
                "candidate_count": 60,
                "queries_attempted": 1,
                "query_variants_planned": 1,
                "stop_reason": "query_frontier_exhausted",
            }
            return [
                {
                    **evidence(str(index)),
                    "caption": f"Coffee workflow {index}",
                    "matched_queries": ["coffee"],
                    "matched_keywords": ["coffee"],
                }
                for index in range(1, 61)
            ]

        async def hydrate_video_candidates(self, page, candidates):
            self.hydrated_batches.append([candidate["id"] for candidate in candidates])

        async def get_comments_for_multiple_videos(
            self,
            page,
            candidates,
            **kwargs,
        ):
            self.comment_batches.append([candidate["id"] for candidate in candidates])
            return {
                candidate["id"]: {
                    "comments": [],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "limit_reached": False,
                    "has_more": False,
                    "transcript_status": "unavailable",
                    "subtitle_no_caption_reason": "not_provided",
                }
                for candidate in candidates
            }

    records, collector, page, browser = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=50,
    )
    integration = FakeIntegration.instance

    assert len(records) == 50
    assert [len(batch) for batch in integration.comment_batches] == [
        20,
        20,
        10,
    ]
    hydrated_ids = {
        post_id for batch in integration.hydrated_batches for post_id in batch
    }
    assert "51" not in hydrated_ids
    assert collector.last_diagnostics["evidence_ready_candidates"] == 50
    assert collector.last_diagnostics["attempted_candidates"] == 50
    assert collector.last_diagnostics["collection_stop_reason"] == "exact_count_reached"
    assert page.closed is True
    assert browser.close_calls == 0


def test_production_collector_uses_replacements_until_exact_ready_count(
    tmp_path,
    monkeypatch,
):
    class FakeIntegration:
        instance = None

        def __init__(self, *, enable_api, persist_session_secrets):
            self.comment_batches = []
            self.last_search_diagnostics = {}
            type(self).instance = self

        async def initialize_api(self, page):
            return True

        async def discover_search_videos(
            self,
            page,
            topic,
            *,
            max_offsets,
            include_related_queries,
            target_count,
        ):
            assert target_count == 100
            assert include_related_queries is False
            self.last_search_diagnostics = {
                "candidate_target": target_count,
                "candidate_count": 70,
                "queries_attempted": 1,
                "query_variants_planned": 1,
                "stop_reason": "query_frontier_exhausted",
            }
            return [
                {
                    **evidence(str(index)),
                    "caption": f"Coffee workflow {index}",
                    "matched_queries": ["coffee"],
                    "matched_keywords": ["coffee"],
                }
                for index in range(1, 71)
            ]

        async def hydrate_video_candidates(self, page, candidates):
            return None

        async def get_comments_for_multiple_videos(
            self,
            page,
            candidates,
            **kwargs,
        ):
            self.comment_batches.append([candidate["id"] for candidate in candidates])
            details = {}
            for candidate in candidates:
                post_id = candidate["id"]
                ready = int(post_id) % 6 != 0
                details[post_id] = {
                    "comments": [],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "limit_reached": False,
                    "has_more": False,
                    "transcript_status": ("unavailable" if ready else ""),
                    "subtitle_no_caption_reason": "not_provided",
                }
            return details

    records, collector, _, _ = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=50,
    )
    ready_records = [
        record for record in records if normalize_evidence(record, topic="coffee")[1]
    ]

    assert len(records) > 50
    assert len(ready_records) == 50
    assert len(records) == 59
    assert [len(batch) for batch in FakeIntegration.instance.comment_batches] == [
        20,
        20,
        16,
        3,
    ]
    assert collector.last_diagnostics["evidence_ready_candidates"] == 50
    assert collector.last_diagnostics["collection_stop_reason"] == "exact_count_reached"


def test_new_only_reserves_lazily_and_replaces_a_rejected_lease(
    tmp_path,
    monkeypatch,
):
    class FakeIntegration:
        instance = None

        def __init__(self, *, enable_api, persist_session_secrets):
            self.hydrated_ids = []
            self.last_search_diagnostics = {}
            type(self).instance = self

        async def initialize_api(self, page):
            return True

        async def discover_search_videos(
            self,
            page,
            topic,
            *,
            max_offsets,
            include_related_queries,
            target_count,
        ):
            assert include_related_queries is False
            ids = ("1", "2", "3", "4", "5")
            self.last_search_diagnostics = {
                "candidate_target": target_count,
                "candidate_count": len(ids),
                "queries_attempted": 1,
                "query_variants_planned": 1,
                "stop_reason": "candidate_target_reached",
            }
            return [
                {
                    **evidence(post_id),
                    "caption": f"Coffee workflow {post_id}",
                }
                for post_id in ids
            ]

        async def hydrate_video_candidates(self, page, candidates):
            self.hydrated_ids.extend(candidate["id"] for candidate in candidates)

        async def get_comments_for_multiple_videos(
            self,
            page,
            candidates,
            **kwargs,
        ):
            return {
                candidate["id"]: {
                    "comments": [],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "limit_reached": False,
                    "has_more": False,
                    "transcript_status": "unavailable",
                    "subtitle_no_caption_reason": "not_provided",
                }
                for candidate in candidates
            }

    reservation_attempts = []

    def reserve(post_id, candidate):
        reservation_attempts.append(post_id)
        return post_id != "1"

    records, collector, _, _ = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=2,
        collector_options={
            "collection_policy": "new_only",
            "candidate_reserver": reserve,
        },
    )

    assert reservation_attempts == ["1", "2", "3"]
    assert FakeIntegration.instance.hydrated_ids == ["2", "3"]
    assert [record["id"] for record in records] == ["2", "3"]
    assert collector.last_diagnostics["reservation_skipped"] == 1


def test_new_only_exact_query_pages_deeper_past_known_candidate_reserve(
    tmp_path,
    monkeypatch,
):
    class FakeIntegration:
        instance = None

        def __init__(self, *, enable_api, persist_session_secrets):
            self.discovery_targets = []
            self.hydrated_ids = []
            self.last_search_diagnostics = {}
            type(self).instance = self

        async def initialize_api(self, page):
            return True

        async def discover_search_videos(
            self,
            page,
            topic,
            *,
            max_offsets,
            include_related_queries,
            target_count,
        ):
            assert include_related_queries is False
            self.discovery_targets.append(target_count)
            ids = (
                ("1", "2", "5", "6")
                if len(self.discovery_targets) == 1
                else (
                    "1",
                    "2",
                    "5",
                    "6",
                    "3",
                    "4",
                )
            )
            self.last_search_diagnostics = {
                "candidate_target": target_count,
                "candidate_count": len(ids),
                "queries_attempted": 1,
                "query_variants_planned": 1,
                "stop_reason": (
                    "candidate_target_reached"
                    if len(self.discovery_targets) == 1
                    else "source_exhausted"
                ),
            }
            return [
                {
                    **evidence(post_id),
                    "caption": f"Coffee workflow {post_id}",
                }
                for post_id in ids
            ]

        async def hydrate_video_candidates(self, page, candidates):
            self.hydrated_ids.extend(candidate["id"] for candidate in candidates)

        async def get_comments_for_multiple_videos(
            self,
            page,
            candidates,
            **kwargs,
        ):
            return {
                candidate["id"]: {
                    "comments": [],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "limit_reached": False,
                    "has_more": False,
                    "transcript_status": "unavailable",
                    "subtitle_no_caption_reason": "not_provided",
                }
                for candidate in candidates
            }

    records, collector, _, _ = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=2,
        collector_options={
            "collection_policy": "new_only",
            "global_known_post_ids": ("1", "2", "5", "6"),
            "candidate_reserver": lambda post_id, candidate: True,
        },
    )

    assert [record["id"] for record in records] == ["3", "4"]
    assert FakeIntegration.instance.hydrated_ids == ["3", "4"]
    assert FakeIntegration.instance.discovery_targets == [4, 8]
    assert collector.last_diagnostics["global_known_skipped"] == 4
    assert collector.last_diagnostics["collection_stop_reason"] == (
        "exact_count_reached"
    )


def test_new_only_exact_query_stops_at_verified_source_exhaustion(
    tmp_path,
    monkeypatch,
):
    class FakeIntegration:
        instance = None

        def __init__(self, *, enable_api, persist_session_secrets):
            self.discovery_targets = []
            self.last_search_diagnostics = {}
            type(self).instance = self

        async def initialize_api(self, page):
            return True

        async def discover_search_videos(
            self,
            page,
            topic,
            *,
            max_offsets,
            include_related_queries,
            target_count,
        ):
            assert include_related_queries is False
            self.discovery_targets.append(target_count)
            self.last_search_diagnostics = {
                "candidate_target": target_count,
                "candidate_count": 2,
                "queries_attempted": 1,
                "query_variants_planned": 1,
                "stop_reason": "source_exhausted",
            }
            return [
                {**evidence(post_id), "caption": f"Coffee workflow {post_id}"}
                for post_id in ("1", "2")
            ]

        async def hydrate_video_candidates(self, page, candidates):
            raise AssertionError("known posts must not be hydrated")

        async def get_comments_for_multiple_videos(self, page, candidates, **kwargs):
            raise AssertionError("known posts must not request comments")

    records, collector, _, _ = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=1,
        collector_options={
            "collection_policy": "new_only",
            "global_known_post_ids": ("1", "2"),
            "candidate_reserver": lambda post_id, candidate: True,
        },
    )

    assert records == []
    assert FakeIntegration.instance.discovery_targets == [2]
    assert collector.last_diagnostics["global_known_skipped"] == 2
    assert collector.last_diagnostics["collection_stop_reason"] == (
        "no_new_candidates"
    )


def test_refresh_known_uses_only_stored_targeted_candidates(
    tmp_path,
    monkeypatch,
):
    class FakeIntegration:
        instance = None

        def __init__(self, *, enable_api, persist_session_secrets):
            self.refreshed_ids = []
            self.last_search_diagnostics = {}
            type(self).instance = self

        async def initialize_api(self, page):
            return True

        async def discover_search_videos(self, *args, **kwargs):
            raise AssertionError("refresh_known must not invoke broad search discovery")

        async def refresh_video_candidates_from_html(
            self,
            page,
            candidates,
        ):
            self.refreshed_ids.extend(candidate["id"] for candidate in candidates)
            for candidate in candidates:
                candidate.update(
                    {
                        "metadata_refresh_ok": True,
                        "metadata_method": "tiktok_web_rehydration",
                        "caption": (f"Fresh coffee workflow {candidate['id']}"),
                        "view_count": 200,
                        "like_count": 20,
                        "comment_count": 1,
                    }
                )
            return {
                "eligible": len(candidates),
                "attempted": len(candidates),
                "hydrated": len(candidates),
                "failed": 0,
            }

        async def get_comments_for_multiple_videos(
            self,
            page,
            candidates,
            **kwargs,
        ):
            return {
                candidate["id"]: {
                    "comments": [],
                    "ok": True,
                    "complete": True,
                    "exhausted": True,
                    "limit_reached": False,
                    "has_more": False,
                    "transcript_status": "unavailable",
                    "subtitle_no_caption_reason": "not_provided",
                }
                for candidate in candidates
            }

    selected = [
        {
            "post_id": post_id,
            "canonical_url": (f"https://www.tiktok.com/@creator/video/{post_id}"),
            "creator": "creator",
            "caption": f"Cached caption {post_id}",
        }
        for post_id in ("21", "22")
    ]
    records, collector, _, _ = run_fake_production_collector(
        tmp_path,
        monkeypatch,
        FakeIntegration,
        requested_count=2,
        collector_options={
            "collection_policy": "refresh_known",
            "refresh_candidates": selected,
            "candidate_reserver": lambda post_id, candidate: True,
        },
    )

    assert FakeIntegration.instance.refreshed_ids == ["21", "22"]
    assert [record["id"] for record in records] == ["21", "22"]
    assert all(record["metadata_refresh_ok"] is True for record in records)
    assert collector.last_diagnostics["selected_refresh_candidates"] == 2
    assert collector.last_diagnostics["collection_stop_reason"] == "exact_count_reached"


def test_failed_registry_metadata_refresh_cannot_count_cached_values():
    stale = {
        **evidence("31"),
        "discovery_method": "master_registry_refresh",
        "metadata_refresh_ok": False,
    }
    packet, ready, issues = normalize_evidence(stale, topic="coffee")

    assert ready is False
    assert packet["evidence_ready"] is False
    assert "targeted_metadata_refresh_not_fresh" in issues


def test_direct_html_refresh_uses_rehydrated_item_and_subtitle_parser():
    from tiktok_scraper.api_integration import TikTokAPIIntegration

    post_id = "61"
    target_url = f"https://www.tiktok.com/@creator/video/{post_id}"
    item = {
        "id": post_id,
        "desc": "Fresh HTML caption",
        "author": {"uniqueId": "creator", "nickname": "Creator"},
        "stats": {
            "playCount": 500,
            "diggCount": 50,
            "commentCount": 5,
            "shareCount": 2,
        },
        "video": {
            "subtitleInfos": [
                {
                    "LanguageCodeName": "eng-US",
                    "Url": "https://v16.tiktokcdn.com/subtitle.vtt",
                    "Format": "webvtt",
                }
            ]
        },
    }
    payload = {
        "__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}
    }
    html = (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        + json.dumps(payload)
        + "</script></html>"
    )

    class FakeResponse:
        ok = True

        async def text(self):
            return html

    class FakeRequest:
        async def get(self, url, **kwargs):
            assert url == target_url
            assert kwargs["timeout"] >= 1000
            return FakeResponse()

    page = SimpleNamespace(request=FakeRequest())
    candidate = {
        "id": post_id,
        "url": target_url,
        "username": "creator",
        "caption": "Stale cached caption",
        "view_count": 1,
    }
    integration = TikTokAPIIntegration(enable_api=False)
    stats = asyncio.run(
        integration.refresh_video_candidates_from_html(
            page,
            [candidate],
        )
    )

    assert stats == {
        "eligible": 1,
        "attempted": 1,
        "hydrated": 1,
        "failed": 0,
    }
    assert candidate["metadata_refresh_ok"] is True
    assert candidate["metadata_method"] == "tiktok_web_rehydration"
    assert candidate["caption"] == "Fresh HTML caption"
    assert candidate["view_count"] == 500
    assert candidate["subtitle_track_count"] == 1
    assert candidate["_tiktok_subtitle_manifest"]["observed"] is True


def approved_publication_for_master_test(
    database,
    tmp_path,
    *,
    run_id,
    project,
    account="creator",
):
    conn = connect_database(database)
    try:
        run_id = advance_to_reviewed(
            conn,
            tmp_path,
            mode="live",
            run_id=run_id,
            project=project,
            account=account,
        )
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        authorize_response(
            conn,
            run_id,
            post["post_id"],
            authorized_by="user@example",
            expected_draft_hash=post["draft_hash"],
            expected_review_hash=post["review_hash"],
            expected_presentation_hash=presentation["presentation_hash"],
            approval_token=presentation["approval_token"],
        )
        handoff = handoff_publication(conn, run_id, post["post_id"])
    finally:
        conn.close()
    return load_approved_publication(database, handoff["publication_id"])


def test_master_comment_history_blocks_same_account_post_across_projects(
    tmp_path,
):
    master = tmp_path / "master.sqlite"
    first_database = tmp_path / "first.sqlite"
    second_database = tmp_path / "second.sqlite"
    other_account_database = tmp_path / "other-account.sqlite"
    first = approved_publication_for_master_test(
        first_database,
        tmp_path,
        run_id="master-run-1",
        project="project-one",
    )
    second = approved_publication_for_master_test(
        second_database,
        tmp_path,
        run_id="master-run-2",
        project="project-two",
    )
    other_account = approved_publication_for_master_test(
        other_account_database,
        tmp_path,
        run_id="master-run-3",
        project="project-three",
        account="other",
    )

    first["observed_account"] = "creator"
    claim_publication(
        first_database,
        first,
        daily_limit=0,
        max_attempts=3,
        master_database=master,
        observed_account="creator",
    )
    mark_publication_submit_intent(
        first_database,
        first,
        master_database=master,
    )
    store_receipt(
        first_database,
        first,
        success=True,
        capture_records=[],
        remote_comment_id="master-comment-1",
        visible=True,
        persisted=True,
        verification_path="verification.png",
        master_database=master,
    )
    master_conn = sqlite3.connect(master)
    try:
        master_run = master_conn.execute(
            """
            SELECT status, published, failed
            FROM tiktok_master_runs
            WHERE local_run_id='master-run-1'
            """
        ).fetchone()
    finally:
        master_conn.close()
    assert master_run == ("published", 1, 0)

    second["observed_account"] = "creator"
    with pytest.raises(RuntimeError, match="Master comment history blocks"):
        claim_publication(
            second_database,
            second,
            daily_limit=0,
            max_attempts=3,
            master_database=master,
            observed_account="creator",
        )

    other_account["observed_account"] = "other"
    claim_publication(
        other_account_database,
        other_account,
        daily_limit=0,
        max_attempts=3,
        master_database=master,
        observed_account="other",
    )
    assert other_account["master_attempt_id"]


def test_master_pre_submit_failure_retries_but_submit_intent_blocks(
    tmp_path,
):
    master = tmp_path / "master.sqlite"
    first_database = tmp_path / "pre-submit.sqlite"
    retry_database = tmp_path / "retry.sqlite"
    blocked_database = tmp_path / "blocked.sqlite"
    first = approved_publication_for_master_test(
        first_database,
        tmp_path,
        run_id="pre-submit-run",
        project="pre-submit-project",
    )
    retry = approved_publication_for_master_test(
        retry_database,
        tmp_path,
        run_id="retry-run",
        project="retry-project",
    )
    blocked = approved_publication_for_master_test(
        blocked_database,
        tmp_path,
        run_id="blocked-run",
        project="blocked-project",
    )

    first["observed_account"] = "creator"
    claim_publication(
        first_database,
        first,
        daily_limit=0,
        max_attempts=3,
        master_database=master,
        observed_account="creator",
    )
    store_receipt(
        first_database,
        first,
        success=False,
        capture_records=[],
        remote_comment_id="",
        visible=False,
        persisted=False,
        verification_path="",
        error="failed before submit intent",
        outcome="failed",
        master_database=master,
    )
    first_conn = sqlite3.connect(first_database)
    try:
        first_status = first_conn.execute(
            """
            SELECT status, attempts
            FROM publication_queue
            WHERE publication_id=?
            """,
            (first["publication_id"],),
        ).fetchone()
    finally:
        first_conn.close()
    assert first_status == ("approved", 1)

    retry["observed_account"] = "creator"
    claim_publication(
        retry_database,
        retry,
        daily_limit=0,
        max_attempts=3,
        master_database=master,
        observed_account="creator",
    )
    mark_publication_submit_intent(
        retry_database,
        retry,
        master_database=master,
    )
    store_receipt(
        retry_database,
        retry,
        success=False,
        capture_records=[],
        remote_comment_id="",
        visible=False,
        persisted=False,
        verification_path="",
        error="submit result could not be confirmed",
        outcome="uncertain",
        master_database=master,
    )
    master_conn = sqlite3.connect(master)
    try:
        master_run = master_conn.execute(
            """
            SELECT status, published, failed
            FROM tiktok_master_runs
            WHERE local_run_id='retry-run'
            """
        ).fetchone()
    finally:
        master_conn.close()
    assert master_run == ("completed_with_failures", 0, 1)
    retry_conn = sqlite3.connect(retry_database)
    try:
        retry_status = retry_conn.execute(
            """
            SELECT status
            FROM publication_queue
            WHERE publication_id=?
            """,
            (retry["publication_id"],),
        ).fetchone()
    finally:
        retry_conn.close()
    assert retry_status == ("uncertain",)

    blocked["observed_account"] = "creator"
    with pytest.raises(RuntimeError, match="Master comment history blocks"):
        claim_publication(
            blocked_database,
            blocked,
            daily_limit=0,
            max_attempts=3,
            master_database=master,
            observed_account="creator",
        )


def test_atomic_claim_and_receipt_update_engage_state(tmp_path):
    database = tmp_path / "state.sqlite"
    conn = connect_database(database)
    try:
        run_id = advance_to_reviewed(conn, tmp_path, mode="live")
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        authorize_response(
            conn,
            run_id,
            post["post_id"],
            authorized_by="user@example",
            expected_draft_hash=post["draft_hash"],
            expected_review_hash=post["review_hash"],
            expected_presentation_hash=presentation["presentation_hash"],
            approval_token=presentation["approval_token"],
        )
        handoff = handoff_publication(conn, run_id, post["post_id"])
    finally:
        conn.close()

    publication = load_approved_publication(database, handoff["publication_id"])
    with pytest.raises(RuntimeError, match="atomic publication claim"):
        store_receipt(
            database,
            publication,
            success=True,
            capture_records=[],
            remote_comment_id="must-not-store",
            visible=True,
            persisted=True,
            verification_path="verification.png",
            master_database=database,
        )
    claim_publication(
        database,
        publication,
        daily_limit=0,
        max_attempts=3,
        master_database=database,
    )
    conn = connect_database(database)
    try:
        claimed = conn.execute(
            "SELECT status, attempts FROM publication_queue WHERE publication_id=?",
            (handoff["publication_id"],),
        ).fetchone()
        assert tuple(claimed) == ("publishing", 1)
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="atomic claim"):
        claim_publication(
            database,
            publication,
            daily_limit=0,
            max_attempts=3,
            master_database=database,
        )

    publication["observed_account"] = "publisher"
    receipt_id = store_receipt(
        database,
        publication,
        success=True,
        capture_records=[],
        remote_comment_id="comment-123",
        visible=True,
        persisted=True,
        verification_path="verification.png",
        master_database=database,
    )
    assert receipt_id
    conn = connect_database(database)
    try:
        queued = conn.execute(
            "SELECT status, attempts FROM publication_queue WHERE publication_id=?",
            (handoff["publication_id"],),
        ).fetchone()
        post_state = conn.execute(
            "SELECT status FROM engage_tiktok_posts WHERE run_id=? AND post_id=?",
            (run_id, post["post_id"]),
        ).fetchone()[0]
        assert tuple(queued) == ("published", 1)
        assert post_state == "published"
        status = run_status(conn, run_id)
        assert status["published"] == 1
        assert status["status"] == "published"
    finally:
        conn.close()


def test_adapter_rejects_drifted_engage_exact_count(tmp_path):
    database = tmp_path / "state.sqlite"
    conn = connect_database(database)
    try:
        run_id = advance_to_reviewed(conn, tmp_path, mode="live")
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        authorize_response(
            conn,
            run_id,
            post["post_id"],
            authorized_by="user@example",
            expected_draft_hash=post["draft_hash"],
            expected_review_hash=post["review_hash"],
            expected_presentation_hash=presentation["presentation_hash"],
            approval_token=presentation["approval_token"],
        )
        handoff = handoff_publication(conn, run_id, post["post_id"])
        conn.execute(
            "UPDATE engage_tiktok_runs SET unique_collected=0 WHERE run_id=?",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="stale"):
        load_approved_publication(database, handoff["publication_id"])
    conn = connect_database(database)
    try:
        assert (
            conn.execute(
                "SELECT status FROM publication_queue WHERE publication_id=?",
                (handoff["publication_id"],),
            ).fetchone()[0]
            == "stale"
        )
        assert (
            conn.execute(
                """
            SELECT status FROM engage_tiktok_posts
            WHERE run_id=? AND post_id=?
            """,
                (run_id, post["post_id"]),
            ).fetchone()[0]
            == "stale"
        )
        status = run_status(conn, run_id)
        assert status["failed"] == 1
        assert status["status"] == "completed_with_failures"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "source_drift",
    ("exact_count", "shadow_mode", "project"),
)
def test_atomic_claim_revalidates_engage_source_after_initial_load(
    tmp_path,
    source_drift,
):
    database = tmp_path / "state.sqlite"
    conn = connect_database(database)
    try:
        run_id = advance_to_reviewed(conn, tmp_path, mode="live")
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="user@example",
        )
        authorize_response(
            conn,
            run_id,
            post["post_id"],
            authorized_by="user@example",
            expected_draft_hash=post["draft_hash"],
            expected_review_hash=post["review_hash"],
            expected_presentation_hash=presentation["presentation_hash"],
            approval_token=presentation["approval_token"],
        )
        handoff = handoff_publication(conn, run_id, post["post_id"])
    finally:
        conn.close()

    publication = load_approved_publication(database, handoff["publication_id"])
    conn = connect_database(database)
    try:
        if source_drift == "exact_count":
            conn.execute(
                "UPDATE engage_tiktok_runs SET unique_collected=0 WHERE run_id=?",
                (run_id,),
            )
        elif source_drift == "shadow_mode":
            conn.execute(
                "UPDATE engage_tiktok_runs SET mode='shadow' WHERE run_id=?",
                (run_id,),
            )
        else:
            conn.execute(
                """
                UPDATE engage_tiktok_runs SET project='different-project'
                WHERE run_id=?
                """,
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="became stale"):
        claim_publication(
            database,
            publication,
            daily_limit=5,
            max_attempts=3,
            master_database=database,
        )

    conn = connect_database(database)
    try:
        queued = conn.execute(
            """
            SELECT status, revalidation_status FROM publication_queue
            WHERE publication_id=?
            """,
            (handoff["publication_id"],),
        ).fetchone()
        post_status = conn.execute(
            """
            SELECT status FROM engage_tiktok_posts
            WHERE run_id=? AND post_id=?
            """,
            (run_id, post["post_id"]),
        ).fetchone()[0]
        assert tuple(queued) == ("stale", "failed")
        assert post_status == "stale"
    finally:
        conn.close()


def test_engage_browser_credentials_remain_in_memory(tmp_path):
    from tiktok_scraper.api_integration import TikTokAPIIntegration

    class FakeContext:
        async def cookies(self):
            return [
                {"name": "sessionid", "value": "sensitive-session-value"},
                {"name": "ttwid", "value": "sensitive-browser-value"},
            ]

    class FakePage:
        context = FakeContext()

        async def evaluate(self, script):
            if "navigator.userAgent" in script:
                return "test-user-agent"
            if "const tokens" in script:
                return {
                    "csrf_token": "sensitive-csrf-value",
                    "verifyFp": "sensitive-verify-value",
                    "has_xbogus": True,
                }
            return "sensitive-ms-token"

    integration = TikTokAPIIntegration(
        enable_api=True,
        persist_session_secrets=False,
    )
    integration.tokens_file = str(tmp_path / "tokens.json")
    integration.cookies_file = str(tmp_path / "cookies.json")
    tokens = asyncio.run(integration.extract_security_tokens_from_browser(FakePage()))
    cookies = asyncio.run(integration.extract_cookies_from_browser(FakePage()))

    assert tokens["msToken"] == "sensitive-ms-token"
    assert cookies["sessionid"] == "sensitive-session-value"
    assert not (tmp_path / "tokens.json").exists()
    assert not (tmp_path / "cookies.json").exists()
