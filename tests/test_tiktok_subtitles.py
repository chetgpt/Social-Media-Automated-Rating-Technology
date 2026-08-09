import asyncio
import json
import sqlite3

from incremental_project import ingest_videos, init_db
from tiktok_scraper.tiktok_subtitles import (
    collect_tiktok_transcript,
    extract_tiktok_subtitle_manifest,
    extract_tiktok_subtitle_manifest_from_html,
    parse_tiktok_timed_text,
    select_tiktok_subtitle_track,
)


VIDEO_ID = "7488734498964720903"
VIDEO_URL = f"https://www.tiktok.com/@creator/video/{VIDEO_ID}"
ID_TRACK_URL = "https://v16-webapp.tiktok.com/subtitles/id-track"
EN_TRACK_URL = "https://v16-webapp.tiktok.com/subtitles/en-track"


def item_payload():
    return {
        "id": VIDEO_ID,
        "video": {
            "subtitleInfos": [
                {
                    "LanguageCodeName": "ind-ID",
                    "Url": ID_TRACK_URL,
                    "Format": "webvtt",
                    "Version": "1:whisper_lid",
                    "Source": "ASR",
                },
                {
                    "LanguageCodeName": "eng-US",
                    "Url": EN_TRACK_URL,
                    "Format": "webvtt",
                    "Version": "4",
                    "Source": "MT",
                },
            ],
            "claInfo": {
                "enableAutoCaption": True,
                "captionInfos": [
                    {
                        "language": "eng-US",
                        "url": EN_TRACK_URL,
                        "captionFormat": "webvtt",
                        "isAutoGen": True,
                        "isOriginalCaption": False,
                        "variant": "default",
                    },
                    {
                        "language": "ind-ID",
                        "url": ID_TRACK_URL,
                        "captionFormat": "webvtt",
                        "isAutoGen": True,
                        "isOriginalCaption": True,
                        "variant": "whisper_lid",
                    },
                ],
            },
        },
    }


VTT = """WEBVTT

00:00:00.120 --> 00:00:01.920
curang kamu pasti curang

00:00:02.120 --> 00:00:03.920 align:start
orang pendaftarannya udah ditutup
"""


class FakeResponse:
    def __init__(self, text, status=200):
        self._text = text
        self.status = status
        self.ok = 200 <= status < 300

    async def text(self):
        return self._text


class FakeRequest:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[url]
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


class FakePage:
    def __init__(self, responses):
        self.request = FakeRequest(responses)


def test_manifest_merges_player_and_cla_track_metadata():
    manifest = extract_tiktok_subtitle_manifest(item_payload())

    assert manifest["observed"] is True
    assert len(manifest["tracks"]) == 2
    indonesian = next(track for track in manifest["tracks"] if track["language"] == "id")
    english = next(track for track in manifest["tracks"] if track["language"] == "en")
    assert indonesian["is_original"] is True
    assert indonesian["is_auto_generated"] is True
    assert indonesian["source"] == "ASR"
    assert english["is_translated"] is True


def test_preferred_language_selects_indonesian_before_english():
    tracks = extract_tiktok_subtitle_manifest(item_payload())["tracks"]

    selected = select_tiktok_subtitle_track(tracks, "id,en")
    selected_english = select_tiktok_subtitle_track(tracks, "en,id")

    assert selected["language"] == "id"
    assert selected_english["language"] == "en"


def test_webvtt_parser_preserves_timestamps_and_text():
    segments = parse_tiktok_timed_text(VTT)

    assert segments == [
        {
            "start": "00:00:00.120",
            "end": "00:00:01.920",
            "start_seconds": 0.12,
            "end_seconds": 1.92,
            "text": "curang kamu pasti curang",
        },
        {
            "start": "00:00:02.120",
            "end": "00:00:03.920",
            "start_seconds": 2.12,
            "end_seconds": 3.92,
            "text": "orang pendaftarannya udah ditutup",
        },
    ]


def test_collector_downloads_preferred_track_and_removes_signed_urls():
    manifest = extract_tiktok_subtitle_manifest(item_payload())
    page = FakePage({ID_TRACK_URL: VTT})

    result = asyncio.run(
        collect_tiktok_transcript(
            page,
            {
                "id": VIDEO_ID,
                "url": VIDEO_URL,
                "_tiktok_subtitle_manifest": manifest,
            },
            preferred_languages="id,en",
            retries=0,
        )
    )

    assert result["transcript_status"] == "ok"
    assert result["transcript_available"] is True
    assert result["transcript_language"] == "id"
    assert result["transcript_segment_count"] == 2
    assert result["transcript"] == (
        "curang kamu pasti curang\norang pendaftarannya udah ditutup"
    )
    assert result["transcript_attempt_count"] == 1
    assert "url" not in result["subtitle_selected_track"]
    assert all("url" not in track for track in result["subtitle_tracks"])


def test_collector_marks_observed_no_caption_without_retrying():
    page = FakePage({})
    result = asyncio.run(
        collect_tiktok_transcript(
            page,
            {
                "id": VIDEO_ID,
                "url": VIDEO_URL,
                "_tiktok_subtitle_manifest": {
                    "observed": True,
                    "tracks": [],
                    "no_caption_reason": 3,
                    "manifest_source": "tiktok_item_payload",
                },
            },
        )
    )

    assert result["transcript_status"] == "unavailable"
    assert result["subtitle_no_caption_reason"] == "3"
    assert page.request.calls == []


def test_collector_can_hydrate_manifest_from_video_html():
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "itemInfo": {"itemStruct": item_payload()}
            }
        }
    }
    page_html = (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        + json.dumps(payload)
        + "</script></html>"
    )
    manifest = extract_tiktok_subtitle_manifest_from_html(page_html, VIDEO_ID)
    assert manifest["manifest_source"] == "tiktok_web_rehydration"
    assert len(manifest["tracks"]) == 2

    page = FakePage({VIDEO_URL: page_html, ID_TRACK_URL: VTT})
    result = asyncio.run(
        collect_tiktok_transcript(
            page,
            {"id": VIDEO_ID, "url": VIDEO_URL},
            preferred_languages="id,en",
            retries=0,
        )
    )
    assert result["transcript_status"] == "ok"
    assert result["transcript_attempt_count"] == 2
    assert [call[0] for call in page.request.calls] == [VIDEO_URL, ID_TRACK_URL]


def test_incremental_store_preserves_timed_segments_and_track_provenance():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    segments = parse_tiktok_timed_text(VTT)
    ingest_videos(
        conn,
        project="project",
        keyword="topic",
        platform="tiktok",
        videos=[
            {
                "video_id": VIDEO_ID,
                "url": VIDEO_URL,
                "caption": "Example",
                "comments": [],
                "transcript": "\n".join(segment["text"] for segment in segments),
                "transcript_available": True,
                "transcript_language": "id",
                "transcript_segment_count": len(segments),
                "transcript_segments": segments,
                "transcript_status": "ok",
                "transcript_source": "tiktok_web_subtitle_api",
                "subtitle_tracks": [{"language": "id", "format": "webvtt"}],
                "subtitle_selected_track": {"language": "id", "format": "webvtt"},
                "subtitle_manifest_source": "tiktok_web_rehydration",
            }
        ],
        run_id="run-tiktok-subtitle",
        scraped_at="2026-07-22T16:00:00+07:00",
    )

    row = conn.execute(
        "SELECT transcript_segments_json, subtitle_tracks_json, "
        "subtitle_selected_track_json, subtitle_manifest_source FROM content_items"
    ).fetchone()
    assert json.loads(row["transcript_segments_json"]) == segments
    assert json.loads(row["subtitle_tracks_json"])[0]["language"] == "id"
    assert json.loads(row["subtitle_selected_track_json"])["format"] == "webvtt"
    assert row["subtitle_manifest_source"] == "tiktok_web_rehydration"
