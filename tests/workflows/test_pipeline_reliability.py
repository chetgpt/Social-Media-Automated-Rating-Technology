import asyncio
import json
import sqlite3
from types import SimpleNamespace

import requests
import youtube_transcript_api
from youtube_transcript_api._errors import IpBlocked

import incremental_project
from comment_compiler import combine_comments, create_minimized_data, infer_video_platform
from incremental_project import (
    ensure_social_browser_session,
    export_known_content_file,
    ingest_videos,
    init_db,
    is_transient_cdp_attach_failure,
    social_browser_is_required,
)
from run_full_pipeline import project_name
from tiktok_api import TikTokAPI
from tiktok_scraper.main import save_comments_to_file
from tiktok_scraper.scrapers.youtube_scraper import YouTubeScraper


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def video_record(video_id, comments):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "caption": f"Video {video_id}",
        "comments": comments,
    }


def test_browser_backed_pipeline_enables_social_browser_by_default():
    args = SimpleNamespace(
        platform="all",
        compile_only=False,
        profile_enrichment=False,
        profile_browser_fallback=True,
    )

    assert social_browser_is_required(args) is True

    args.platform = "youtube"
    assert social_browser_is_required(args) is False

    args.compile_only = True
    args.profile_enrichment = True
    assert social_browser_is_required(args) is True


def test_ensure_social_browser_starts_designated_profile_and_reads_live_endpoint(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "social_browser" / "state.json"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "cdp_url": "ws://127.0.0.1:9222/devtools/browser/live",
                    "profile_verified": True,
                    "profile_directory": "Profile 7",
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(incremental_project.subprocess, "run", fake_run)
    args = SimpleNamespace(
        social_browser_cdp_url="",
        social_browser_state=str(state_path),
        social_browser_startup_timeout=60,
    )

    cdp_url = ensure_social_browser_session(args)

    assert cdp_url == "ws://127.0.0.1:9222/devtools/browser/live"
    assert calls[0][0][2] == "start"
    assert "--no-open-tabs" in calls[0][0]


def test_transient_cdp_attach_failure_is_narrowly_detected(tmp_path):
    transient_log = tmp_path / "transient.log"
    transient_log.write_text(
        "BrowserType.connect_over_cdp: Connection closed while reading from the driver",
        encoding="utf-8",
    )
    auth_log = tmp_path / "auth.log"
    auth_log.write_text("The selected X browser session is not authenticated", encoding="utf-8")

    assert is_transient_cdp_attach_failure(transient_log) is True
    assert is_transient_cdp_attach_failure(auth_log) is False


def test_comment_refresh_updates_mutable_fields_and_preserves_first_seen():
    conn = memory_db()
    original = {
        "comment_id": "comment-1",
        "author": "author",
        "text": "first text",
        "likes": 1,
        "reply_count": 0,
    }
    first = ingest_videos(
        conn,
        project="project",
        keyword="topic",
        platform="youtube",
        videos=[video_record("video-1", [original])],
        run_id="run-1",
        scraped_at="2026-07-10T10:00:00",
    )

    refreshed = dict(original, text="edited text", likes=99, reply_count=7)
    second = ingest_videos(
        conn,
        project="project",
        keyword="topic",
        platform="youtube",
        videos=[video_record("video-1", [refreshed])],
        run_id="run-2",
        scraped_at="2026-07-11T10:00:00",
    )

    row = conn.execute(
        "SELECT text, likes, reply_count, first_seen_at, scraped_at, last_seen_at FROM comments"
    ).fetchone()
    assert first["new_comment_count"] == 1
    assert second["new_comment_count"] == 0
    assert dict(row) == {
        "text": "edited text",
        "likes": 99,
        "reply_count": "7",
        "first_seen_at": "2026-07-10T10:00:00",
        "scraped_at": "2026-07-10T10:00:00",
        "last_seen_at": "2026-07-11T10:00:00",
    }


def test_known_content_export_schedules_bounded_recent_refresh(tmp_path):
    conn = memory_db()
    for index, timestamp in enumerate(("2026-01-01T00:00:00", "2026-02-01T00:00:00"), start=1):
        ingest_videos(
            conn,
            project="project",
            keyword="topic",
            platform="youtube",
            videos=[video_record(f"video-{index}", [])],
            run_id=f"run-{index}",
            scraped_at=timestamp,
        )

    output = export_known_content_file(
        conn,
        "project",
        "youtube",
        {"state": tmp_path},
        refresh_limit=1,
        refresh_after_hours=1,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["refresh_scheduled_count"] == 1
    assert payload["refresh_content_keys"] == ["video-2"]
    assert "video-1" in payload["keys"]
    assert "video-2" not in payload["keys"]


def test_platform_prefixed_output_is_explicit_and_isolated(tmp_path):
    output = asyncio.run(
        save_comments_to_file(
            [{"id": "comment-1", "text": "hello"}],
            1,
            str(tmp_path),
            video_id="video-1",
            filename_prefix="tiktok_",
            platform="tiktok",
        )
    )
    payload = json.loads((tmp_path / "tiktok_video_1_comments.json").read_text(encoding="utf-8"))
    assert output.endswith("tiktok_video_1_comments.json")
    assert payload["platform"] == "tiktok"


def test_compiler_deduplicates_aggregate_and_per_video_records(tmp_path):
    aggregate = {
        "videos": [
            {
                "platform": "youtube",
                "video_id": "youtube-id",
                "url": "https://www.youtube.com/watch?v=youtube-id",
                "comments": [{"id": "one", "text": "one"}],
            },
            {
                "video_id": "InstagramShortcode",
                "url": "https://www.instagram.com/reel/InstagramShortcode/",
                "comments": [],
            },
        ]
    }
    duplicate = aggregate["videos"][0]
    (tmp_path / "youtube_comments.json").write_text(json.dumps(aggregate), encoding="utf-8")
    (tmp_path / "youtube_video_1_comments.json").write_text(json.dumps(duplicate), encoding="utf-8")

    combined, error = combine_comments(str(tmp_path))
    assert error == ""
    assert combined["total_videos"] == 2
    assert infer_video_platform(aggregate["videos"][1]) == "instagram"
    assert infer_video_platform({"video_id": "non-numeric-but-unknown"}) == ""


def test_compiler_counts_and_minimizes_nested_replies(tmp_path):
    aggregate = {
        "videos": [{
            "platform": "x",
            "video_id": "x-post",
            "url": "https://x.com/example/status/x-post",
            "comments": [{
                "comment_id": "reply-1",
                "text": "top level",
                "author": "reader",
                "likes": 2,
                "replies": [{
                    "comment_id": "reply-2",
                    "parent_comment_id": "reply-1",
                    "text": "nested",
                    "author": "publisher",
                    "likes": 1,
                    "replies": [],
                }],
            }],
        }]
    }
    (tmp_path / "x_comments.json").write_text(json.dumps(aggregate), encoding="utf-8")

    combined, error = combine_comments(str(tmp_path))
    minimized = create_minimized_data(combined)

    assert error == ""
    assert combined["total_comments"] == 2
    assert combined["videos"][0]["comment_count"] == 2
    assert [comment["id"] for comment in minimized["videos"][0]["comments"]] == [
        "reply-1",
        "reply-2",
    ]
    assert minimized["videos"][0]["comments"][1]["parent_comment_id"] == "reply-1"
    assert minimized["videos"][0]["comments"][1]["reply_depth"] == 1


def test_legacy_pipeline_uses_a_stable_project_name():
    assert project_name("Operasi Pesta Copet!") == "operasi_pesta_copet"


def test_youtube_exception_path_returns_an_error_record():
    scraper = YouTubeScraper()

    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    scraper._extract_comments_sync = fail
    result = asyncio.run(
        scraper.extract_comments(
            {"url": "https://www.youtube.com/watch?v=test", "video_id": "test"}
        )
    )
    assert result["comments"] == []
    assert result["comment_error"] == "boom"


def test_youtube_transcript_client_selects_preferred_language(monkeypatch):
    class FakeFetched:
        language_code = "id"
        language = "Indonesian"
        is_generated = True

        def to_raw_data(self):
            return [
                {"text": "baris pertama", "start": 0.0, "duration": 1.0},
                {"text": "baris kedua", "start": 1.0, "duration": 1.0},
            ]

    class FakeTrack:
        def __init__(self, language_code, generated):
            self.language_code = language_code
            self.is_generated = generated

        def fetch(self):
            return FakeFetched()

    class FakeClient:
        def __init__(self, http_client):
            self.http_client = http_client

        def list(self, video_id):
            assert video_id == "video-id"
            return [FakeTrack("en", False), FakeTrack("id", True)]

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", FakeClient)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_LANGS", "id,en")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_DELAY_SECONDS", "0")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_RETRIES", "0")

    result = YouTubeScraper()._extract_transcript_with_client_sync(
        "https://www.youtube.com/watch?v=video-id",
        requests.Session(),
    )

    assert result["transcript"] == "baris pertama\nbaris kedua"
    assert result["transcript_language"] == "id"
    assert result["transcript_is_auto_generated"] is True
    assert result["transcript_segment_count"] == 2
    assert result["transcript_status"] == "ok"
    assert result["transcript_source"] == "youtube_transcript_api"


def test_youtube_transcript_ip_block_opens_shared_circuit(monkeypatch):
    calls = {"count": 0}

    class BlockedClient:
        def __init__(self, http_client):
            self.http_client = http_client

        def list(self, video_id):
            calls["count"] += 1
            raise IpBlocked(video_id)

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", BlockedClient)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_DELAY_SECONDS", "0")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_RETRIES", "0")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_COOLDOWN_SECONDS", "60")
    scraper = YouTubeScraper()

    first = scraper._extract_transcript_with_client_sync(
        "https://www.youtube.com/watch?v=blocked-one",
        requests.Session(),
    )
    second = scraper._extract_transcript_with_client_sync(
        "https://www.youtube.com/watch?v=blocked-two",
        requests.Session(),
    )

    assert first["transcript_status"] == "rate_limited"
    assert second["transcript_status"] == "circuit_open"
    assert calls["count"] == 1


def test_youtube_transcript_survives_metadata_failure(monkeypatch):
    scraper = YouTubeScraper()

    def fail_player(*args, **kwargs):
        raise RuntimeError("metadata unavailable")

    def transcript_result(player_response, session, video_url=""):
        assert player_response == {}
        return {
            "transcript": "still available",
            "transcript_available": True,
            "transcript_language": "id",
            "transcript_language_name": "Indonesian",
            "transcript_is_auto_generated": False,
            "transcript_segment_count": 1,
            "transcript_error": "",
            "transcript_status": "ok",
            "transcript_source": "youtube_transcript_api",
            "transcript_attempt_count": 1,
        }

    monkeypatch.setattr(scraper, "_fetch_player_response_sync", fail_player)
    monkeypatch.setattr(scraper, "_extract_transcript_from_player_sync", transcript_result)
    metadata = scraper._extract_video_metadata_sync(
        video_record("video-id", []),
        requests.Session(),
    )

    assert metadata["transcript"] == "still available"
    assert metadata["transcript_available"] is True
    assert "metadata unavailable" in metadata["metadata_error"]


def test_incremental_database_persists_transcript_diagnostics():
    conn = memory_db()
    video = video_record("video-id", [])
    video.update({
        "transcript": "isi transkrip",
        "transcript_available": True,
        "transcript_status": "ok",
        "transcript_source": "youtube_transcript_api",
        "transcript_attempt_count": 1,
    })
    ingest_videos(
        conn,
        project="project",
        keyword="topic",
        platform="youtube",
        videos=[video],
        run_id="run-transcript",
        scraped_at="2026-07-11T12:00:00",
    )

    row = conn.execute(
        "SELECT transcript_status, transcript_source, transcript_attempt_count FROM content_items"
    ).fetchone()
    assert dict(row) == {
        "transcript_status": "ok",
        "transcript_source": "youtube_transcript_api",
        "transcript_attempt_count": 1,
    }


def test_tiktok_comment_status_distinguishes_exhausted_from_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = TikTokAPI()
    api.COMMENT_PAGE_CONCURRENCY = 1

    async def exhausted_page(*args, **kwargs):
        return {"comments": [], "has_more": False, "cursor": "0"}

    api.get_comments = exhausted_page
    exhausted = asyncio.run(
        api.get_all_comments("video-zero", include_replies=False, return_status=True)
    )
    assert exhausted["ok"] is True
    assert exhausted["complete"] is True
    assert exhausted["exhausted"] is True

    async def failed_page(*args, **kwargs):
        return {"comments": [], "has_more": False, "cursor": "0", "error": True}

    api.get_comments = failed_page
    failed = asyncio.run(
        api.get_all_comments("video-failed", include_replies=False, return_status=True)
    )
    assert failed["ok"] is False
    assert failed["complete"] is False
    assert failed["error"]
