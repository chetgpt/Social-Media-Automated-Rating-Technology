import asyncio
import hashlib
import json
import math
import wave
from pathlib import Path

import pytest

import sonic_audio_transport as sonic


POST_ID = "7673070116011216148"
CREATOR = "bankbca"
RUN_ID = "sonic_0123456789abcdef"
OTHER_RUN_ID = "sonic_fedcba9876543210"


def candidate(
    post_id=POST_ID,
    creator=CREATOR,
    *,
    url=None,
    content_type="video",
):
    return {
        "post_id": post_id,
        "canonical_url": url
        or f"https://www.tiktok.com/@{creator}/video/{post_id}",
        "creator_handle": creator,
        "content_type": content_type,
    }


def tiktok_html(
    *,
    post_id=POST_ID,
    creator=CREATOR,
    media_url="https://v16.tiktokcdn.com/transient/video.mp4?signature=secret",
):
    item = {
        "id": post_id,
        "author": {"uniqueId": creator},
        "video": {"playAddr": media_url, "duration": 2},
    }
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {"itemInfo": {"itemStruct": item}}
        }
    }
    return (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


class FakeResponse:
    def __init__(self, html, *, ok=True, headers=None):
        self._html = html
        self.ok = ok
        self.headers = headers or {}
        self.disposed = False

    async def text(self):
        return self._html

    async def dispose(self):
        self.disposed = True


class FakeRequest:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakePage:
    def __init__(self, response):
        self.request = FakeRequest(response)
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class QueuedRequest:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class QueuedPage(FakePage):
    def __init__(self, responses):
        self.request = QueuedRequest(responses)
        self.closed = False


class NavigationPage(FakePage):
    def __init__(self, response):
        super().__init__(response)
        self.navigation_calls = []
        self.selector_calls = []

    async def goto(self, url, **kwargs):
        self.navigation_calls.append((url, kwargs))
        return self.request.response

    async def wait_for_selector(self, selector, **kwargs):
        self.selector_calls.append((selector, kwargs))
        return object()

    async def content(self):
        return self.request.response._html


class FileFetcher:
    def __init__(self, payload=b"bounded source media"):
        self.payload = payload
        self.seen_url = ""
        self.seen_referer = ""

    async def __call__(self, media_url, destination, *, referer):
        self.seen_url = media_url
        self.seen_referer = referer
        destination.write_bytes(self.payload)
        return sonic.DownloadReceipt(
            len(self.payload),
            hashlib.sha256(self.payload).hexdigest(),
            "video/mp4",
        )


class FixedInspector:
    def __init__(self, duration=2.0):
        self.duration = duration

    def __call__(self, path):
        return sonic.MediaProbe(self.duration, path.stat().st_size)


class WaveTranscoder:
    def __init__(self, duration=2.0):
        self.duration = duration
        self.called = False

    def __call__(self, source, destination):
        self.called = True
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * int(16000 * self.duration))


def frozen(value=None, *, limits=None):
    return sonic.validate_frozen_candidates(
        [value or candidate()], limits=limits or sonic.AcquisitionLimits()
    )


def transport(tmp_path, response=None, **kwargs):
    return sonic.SonicAudioTransport(
        temp_root=tmp_path,
        media_fetcher=kwargs.pop("media_fetcher", FileFetcher()),
        media_inspector=kwargs.pop("media_inspector", FixedInspector()),
        audio_transcoder=kwargs.pop("audio_transcoder", WaveTranscoder()),
        **kwargs,
    ), FakePage(response or FakeResponse(tiktok_html()))


def run_with_page(instance, page, processor, *, candidates=None):
    return asyncio.run(
        instance._process_with_verified_page(
            run_id=RUN_ID,
            page=page,
            candidates=candidates or frozen(limits=instance.limits),
            processor=processor,
            observed_account="operator",
        )
    )


def test_candidate_identity_is_bound_before_browser_access():
    with pytest.raises(sonic.CandidateIdentityError, match="post_id_mismatch"):
        sonic.validate_frozen_candidates(
            [
                candidate(
                    url="https://www.tiktok.com/@bankbca/video/1111111111111111111"
                )
            ],
            limits=sonic.AcquisitionLimits(),
        )

    with pytest.raises(sonic.CandidateIdentityError, match="creator_mismatch"):
        sonic.validate_frozen_candidates(
            [candidate(url=f"https://www.tiktok.com/@other/video/{POST_ID}")],
            limits=sonic.AcquisitionLimits(),
        )


def test_pilot_cardinality_is_hard_capped_at_60():
    candidates = [
        candidate(str(7000000000000000000 + index)) for index in range(61)
    ]
    with pytest.raises(sonic.TransportLimitError, match="pilot_post_limit"):
        sonic.validate_frozen_candidates(
            candidates,
            limits=sonic.AcquisitionLimits(),
        )
    with pytest.raises(ValueError, match="safe pilot bound"):
        sonic.AcquisitionLimits(max_posts=61)


def test_browser_free_cleanup_removes_only_closed_exact_run_residue(tmp_path):
    stale = sonic._create_owned_run_directory(tmp_path, RUN_ID)
    marker = json.loads(
        (stale / ".sonic-audit-owner.json").read_text(encoding="utf-8")
    )
    assert marker["run_id"] == RUN_ID
    assert marker["ownership_state"] == "closed"
    raw_dir = stale / "item-hard-kill"
    raw_dir.mkdir()
    (raw_dir / "source-media.bin").write_bytes(b"raw bytes from interrupted run")

    assert sonic.verify_no_run_residue(RUN_ID, temp_root=tmp_path) is False
    assert sonic.cleanup_stale_run(RUN_ID, temp_root=tmp_path) is True
    assert not stale.exists()
    assert sonic.verify_no_run_residue(RUN_ID, temp_root=tmp_path) is True


def test_cleanup_leaves_other_run_and_unmarked_exact_prefix_untouched(tmp_path):
    other = sonic._create_owned_run_directory(tmp_path, OTHER_RUN_ID)
    (other / "other-run-raw.bin").write_bytes(b"other run")
    unmarked = tmp_path / f"sonic-audit-{RUN_ID}-unmarked"
    unmarked.mkdir()
    (unmarked / "source-media.bin").write_bytes(b"cannot prove ownership")

    assert sonic.cleanup_stale_run(RUN_ID, temp_root=tmp_path) is False
    assert unmarked.is_dir()
    assert (unmarked / "source-media.bin").read_bytes() == b"cannot prove ownership"
    assert other.is_dir()
    assert (other / "other-run-raw.bin").read_bytes() == b"other run"
    assert sonic.verify_no_run_residue(RUN_ID, temp_root=tmp_path) is False


def test_unmarked_hard_kill_residue_blocks_before_browser_preflight(tmp_path):
    residue = tmp_path / f"sonic-audit-{RUN_ID}-suspicious"
    residue.mkdir()
    (residue / "source-media.bin").write_bytes(b"do not delete without marker")

    class TrackingPreflight:
        called = False

        async def ensure_ready(self):
            self.called = True
            raise AssertionError("residue gate must run before browser preflight")

    preflight = TrackingPreflight()
    instance = sonic.SonicAudioTransport(temp_root=tmp_path, preflight=preflight)
    with pytest.raises(sonic.SonicTransportError, match="stale_run_residue"):
        asyncio.run(
            instance.process_candidates(
                run_id=RUN_ID,
                candidates=[candidate()],
                processor=lambda item: {},
            )
        )
    assert preflight.called is False
    assert residue.is_dir()
    assert instance.last_cleanup_verified is False


def test_response_post_identity_mismatch_fails_and_cleans(tmp_path):
    response = FakeResponse(tiktok_html(post_id="7000000000000000001"))
    instance, page = transport(tmp_path, response=response)
    with pytest.raises(sonic.CandidateIdentityError, match="post_id_mismatch"):
        run_with_page(instance, page, lambda item: {})
    assert response.disposed is True
    assert list(tmp_path.iterdir()) == []


def test_real_page_shape_uses_navigation_to_materialize_item_metadata(tmp_path):
    instance, _ = transport(tmp_path)
    response = FakeResponse(tiktok_html())
    page = NavigationPage(response)

    receipts = run_with_page(instance, page, lambda item: {"ok": True})

    assert receipts[0].status == "completed"
    assert len(page.navigation_calls) == 1
    assert page.request.calls == []
    assert page.selector_calls == []
    assert response.disposed is True
    assert list(tmp_path.iterdir()) == []


def test_response_creator_mismatch_fails_and_cleans(tmp_path):
    response = FakeResponse(tiktok_html(creator="not_bankbca"))
    instance, page = transport(tmp_path, response=response)
    with pytest.raises(sonic.CandidateIdentityError, match="creator_mismatch"):
        run_with_page(instance, page, lambda item: {})
    assert response.disposed is True
    assert list(tmp_path.iterdir()) == []


def test_callback_streams_paths_then_deletes_all_raw_media(tmp_path):
    fetcher = FileFetcher()
    instance, page = transport(tmp_path, media_fetcher=fetcher)
    seen = {}

    def processor(item):
        assert item.source_media_path.is_file()
        assert item.audio_path.is_file()
        seen["media"] = item.source_media_path
        seen["audio"] = item.audio_path
        seen["provenance"] = item.provenance
        return {"embedding": [0.25, -0.5], "model": "test-model"}

    receipts = run_with_page(instance, page, processor)

    assert len(receipts) == 1
    assert receipts[0].post_id == POST_ID
    assert receipts[0].derived_result["embedding"] == [0.25, -0.5]
    assert receipts[0].provenance["cookies_forwarded_to_media"] is False
    assert receipts[0].provenance["signed_url_retained"] is False
    assert not seen["media"].exists()
    assert not seen["audio"].exists()
    assert list(tmp_path.iterdir()) == []
    assert fetcher.seen_referer == f"https://www.tiktok.com/@{CREATOR}/video/{POST_ID}"
    assert "signature=secret" in fetcher.seen_url
    assert "secret" not in repr(receipts)
    assert set(receipts[0].timing) == {
        "html_fetch_ms",
        "media_download_ms",
        "inspect_transcode_ms",
        "processor_ms",
        "total_item_ms",
    }
    assert all(
        math.isfinite(value) and value >= 0.0
        for value in receipts[0].timing.values()
    )
    assert instance.last_cleanup_verified is True


def test_callback_error_still_deletes_media_audio_and_run_directory(tmp_path):
    instance, page = transport(tmp_path)
    observed_paths = []

    def processor(item):
        observed_paths.extend([item.source_media_path, item.audio_path])
        raise RuntimeError("processor failed")

    with pytest.raises(RuntimeError, match="processor failed"):
        run_with_page(instance, page, processor)
    assert all(not path.exists() for path in observed_paths)
    assert list(tmp_path.iterdir()) == []
    assert instance.last_cleanup_verified is True


def test_explicit_continuation_records_safe_error_then_processes_next_post(tmp_path):
    second_post_id = "7673070116011216149"
    first_response = FakeResponse(
        tiktok_html(
            media_url=(
                "https://untrusted.example/private.mp4?"
                "authorization=must-never-escape"
            )
        )
    )
    second_response = FakeResponse(tiktok_html(post_id=second_post_id))
    page = QueuedPage([first_response, second_response])
    instance = sonic.SonicAudioTransport(
        temp_root=tmp_path,
        media_fetcher=FileFetcher(),
        media_inspector=FixedInspector(),
        audio_transcoder=WaveTranscoder(),
    )
    error_calls = []
    successful = []

    def processor(item):
        successful.append(item.post_id)
        return {"vector": [0.1, 0.2]}

    def on_error(identity, error_code):
        assert isinstance(identity, sonic.SafeCandidateIdentity)
        assert not hasattr(identity, "canonical_url")
        error_calls.append((identity, error_code))
        return {"recorded": True, "error_code": error_code}

    receipts = asyncio.run(
        instance._process_with_verified_page(
            run_id=RUN_ID,
            page=page,
            candidates=sonic.validate_frozen_candidates(
                [candidate(), candidate(second_post_id)],
                limits=instance.limits,
            ),
            processor=processor,
            observed_account="operator",
            continue_on_item_error=True,
            error_processor=on_error,
        )
    )

    assert [receipt.status for receipt in receipts] == [
        "unavailable",
        "completed",
    ]
    assert receipts[0].error_code == "tiktok_item_has_no_safe_video_transport"
    assert receipts[0].derived_result == {
        "recorded": True,
        "error_code": "tiktok_item_has_no_safe_video_transport",
    }
    assert successful == [second_post_id]
    assert len(error_calls) == 1
    assert error_calls[0][0].post_id == POST_ID
    assert "authorization" not in repr(receipts).casefold()
    assert "must-never-escape" not in repr(receipts)
    assert all(
        math.isfinite(value) and value >= 0.0
        for receipt in receipts
        for value in receipt.timing.values()
    )
    assert first_response.disposed is True
    assert second_response.disposed is True
    assert list(tmp_path.iterdir()) == []
    assert instance.last_cleanup_verified is True


def test_receipt_processor_runs_after_item_cleanup_before_next_post(tmp_path):
    second_post_id = "7673070116011216150"
    responses = [
        FakeResponse(tiktok_html()),
        FakeResponse(tiktok_html(post_id=second_post_id)),
    ]
    page = QueuedPage(responses)
    instance = sonic.SonicAudioTransport(
        temp_root=tmp_path,
        media_fetcher=FileFetcher(),
        media_inspector=FixedInspector(),
        audio_transcoder=WaveTranscoder(),
    )
    transient_paths = {}
    checkpoints = []

    def processor(item):
        transient_paths[item.post_id] = (
            item.source_media_path,
            item.audio_path,
        )
        return {"vector": [0.3, 0.4]}

    def checkpoint(receipt):
        source_path, audio_path = transient_paths[receipt.post_id]
        assert not source_path.exists()
        assert not audio_path.exists()
        assert len(page.request.calls) == len(checkpoints) + 1
        assert "path" not in repr(receipt).casefold()
        checkpoints.append(receipt.post_id)

    receipts = asyncio.run(
        instance._process_with_verified_page(
            run_id=RUN_ID,
            page=page,
            candidates=sonic.validate_frozen_candidates(
                [candidate(), candidate(second_post_id)],
                limits=instance.limits,
            ),
            processor=processor,
            observed_account="operator",
            receipt_processor=checkpoint,
        )
    )

    assert checkpoints == [POST_ID, second_post_id]
    assert [receipt.post_id for receipt in receipts] == checkpoints
    assert list(tmp_path.iterdir()) == []
    assert instance.last_cleanup_verified is True


def test_derived_result_cannot_leak_temp_paths_or_urls(tmp_path):
    instance, page = transport(tmp_path)

    def processor(item):
        return {"audio_path": str(item.audio_path)}

    with pytest.raises(
        sonic.SonicTransportError,
        match="derived_result_contains_transport_field",
    ):
        run_with_page(instance, page, processor)
    assert list(tmp_path.iterdir()) == []


def test_source_byte_bound_is_rechecked_after_custom_fetcher(tmp_path):
    limits = sonic.AcquisitionLimits(max_source_bytes=16)
    fetcher = FileFetcher(b"x" * 17)
    instance, page = transport(tmp_path, limits=limits, media_fetcher=fetcher)
    with pytest.raises(sonic.TransportLimitError, match="temporary_file_size"):
        run_with_page(instance, page, lambda item: {})
    assert list(tmp_path.iterdir()) == []


def test_duration_bound_stops_before_transcode_and_cleans(tmp_path):
    transcoder = WaveTranscoder()
    instance, page = transport(
        tmp_path,
        media_inspector=FixedInspector(duration=181.0),
        audio_transcoder=transcoder,
    )
    with pytest.raises(sonic.TransportLimitError, match="duration_limit"):
        run_with_page(instance, page, lambda item: {})
    assert transcoder.called is False
    assert list(tmp_path.iterdir()) == []


class StreamingResponse:
    def __init__(self, chunks, *, headers=None, status_code=200):
        self._chunks = chunks
        self.headers = headers or {"Content-Type": "video/mp4"}
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class AsyncRangeResponse:
    def __init__(self, payload, start, end, *, status=206):
        self.status = status
        self._body = payload[start : end + 1]
        self.headers = {
            "content-type": "video/mp4",
            "content-range": f"bytes {start}-{end}/{len(payload)}",
        }
        self.disposed = False

    async def body(self):
        return self._body

    async def dispose(self):
        self.disposed = True


class AsyncRangeContext:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.responses = []

    async def get(self, url, *, headers, **kwargs):
        match = sonic.re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"])
        assert match
        start, end = map(int, match.groups())
        response = AsyncRangeResponse(self.payload, start, end)
        self.calls.append((url, dict(headers), dict(kwargs)))
        self.responses.append(response)
        return response


def test_authenticated_playwright_fetcher_streams_bounded_ranges(tmp_path):
    payload = bytes(range(251)) * 200
    limits = sonic.AcquisitionLimits(
        max_source_bytes=len(payload) + 1,
        stream_chunk_bytes=1024,
    )
    context = AsyncRangeContext(payload)
    fetcher = sonic.PlaywrightBoundedMediaFetcher(context, limits)
    destination = tmp_path / "source.bin"

    receipt = asyncio.run(
        fetcher(
            "https://v16-webapp-prime.tiktok.com/video?id=signed",
            destination,
            referer=f"https://www.tiktok.com/@{CREATOR}/video/{POST_ID}",
        )
    )

    assert destination.read_bytes() == payload
    assert receipt.byte_count == len(payload)
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert len(context.calls) > 2
    assert all(response.disposed for response in context.responses)
    assert all(call[1]["Range"].startswith("bytes=") for call in context.calls)


def test_default_stream_fetcher_rejects_declared_and_actual_oversize(
    tmp_path,
    monkeypatch,
):
    import requests

    limits = sonic.AcquisitionLimits(max_source_bytes=16)
    fetcher = sonic.RequestsBoundedMediaFetcher(limits)
    declared = StreamingResponse(
        [],
        headers={"Content-Type": "video/mp4", "Content-Length": "17"},
    )
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: declared)
    destination = tmp_path / "declared.bin"
    with pytest.raises(sonic.TransportLimitError, match="content_length"):
        asyncio.run(
            fetcher(
                "https://v16.tiktokcdn.com/media.mp4?signature=never-persist",
                destination,
                referer=f"https://www.tiktok.com/@{CREATOR}/video/{POST_ID}",
            )
        )
    assert declared.closed is True
    assert not destination.exists()

    streamed = StreamingResponse([b"x" * 10, b"y" * 7])
    seen_headers = {}

    def fake_get(*args, **kwargs):
        seen_headers.update(kwargs["headers"])
        return streamed

    monkeypatch.setattr(requests, "get", fake_get)
    destination = tmp_path / "streamed.bin"
    with pytest.raises(sonic.TransportLimitError, match="stream_size"):
        asyncio.run(
            fetcher(
                "https://v16.tiktokcdn.com/media.mp4?signature=never-persist",
                destination,
                referer=f"https://www.tiktok.com/@{CREATOR}/video/{POST_ID}",
            )
        )
    assert streamed.closed is True
    assert not destination.exists()
    assert "Cookie" not in seen_headers
    assert "Authorization" not in seen_headers


class FakePreflight:
    async def ensure_ready(self):
        return {
            "profile": {"verified": True, "profile_directory": "Profile 7"},
            "observed_account": "operator",
        }


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.context = context
        self.close_called = False

    async def close(self):
        self.close_called = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.cdp_url = ""

    async def connect_over_cdp(self, cdp_url):
        self.cdp_url = cdp_url
        return self.browser


class FakePlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright
        self.exited = False

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, *args):
        self.exited = True


class FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def test_production_entry_revalidates_profile7_and_never_closes_browser(tmp_path):
    page = FakePage(FakeResponse(tiktok_html()))
    context = FakeContext(page)
    browser = FakeBrowser(context)
    playwright = type("Playwright", (), {"chromium": FakeChromium(browser)})()
    manager = FakePlaywrightManager(playwright)
    verification_calls = []
    authentication_calls = []

    async def verify(received_browser, designation):
        verification_calls.append((received_browser, designation))
        return context, {"verified": True}

    async def authenticate(received_context, platform):
        authentication_calls.append((received_context, platform))
        return {"authenticated": True}

    instance = sonic.SonicAudioTransport(
        temp_root=tmp_path,
        preflight=FakePreflight(),
        media_fetcher=FileFetcher(),
        media_inspector=FixedInspector(),
        audio_transcoder=WaveTranscoder(),
        playwright_manager_factory=lambda: manager,
        cdp_url_provider=lambda: "ws://127.0.0.1:9222/devtools/browser/verified",
        designation_loader=lambda runtime: {"profile_directory": "Profile 7"},
        profile_context_verifier=verify,
        authentication_checker=authenticate,
    )

    receipts = asyncio.run(
        instance.process_candidates(
            run_id=RUN_ID,
            candidates=[candidate()],
            processor=lambda item: {"vector": [1.0, 0.0]},
            expected_account="@operator",
        )
    )

    assert len(receipts) == 1
    assert verification_calls == [
        (browser, {"profile_directory": "Profile 7"})
    ]
    assert authentication_calls == [(context, "tiktok")]
    assert playwright.chromium.cdp_url.startswith("ws://127.0.0.1:")
    assert page.closed is True
    assert browser.close_called is False
    assert manager.exited is True
    assert list(tmp_path.iterdir()) == []
    assert instance.last_cleanup_verified is True
    assert sonic.verify_no_run_residue(RUN_ID, temp_root=tmp_path) is True
    assert set(instance.last_run_timing) == {
        "preflight_ms",
        "attach_ms",
        "transport_ms",
        "total_run_ms",
    }
    assert all(
        math.isfinite(value) and value >= 0.0
        for value in instance.last_run_timing.values()
    )
    assert "secret" not in repr(instance.last_run_timing).casefold()
