from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from publish_comment_showcase import (
    ClaimedShowcase,
    HostedMediaChecker,
    PreparedShowcase,
    Profile7RemotePostVerifier,
    SQLiteShowcaseBackend,
    ShowcasePublishWorkerError,
    load_access_token,
    publish_authorized_showcase,
    publish_showcase_batch,
    reconcile_interrupted_showcase,
    require_url_under_verified_base,
    stage_api_ready_jpeg,
)


MEDIA_URL = "https://media.example.test/showcases/showcase-1.jpg"
MEDIA_HASH = "a" * 64
CAPTION_HASH = "b" * 64
AUTHORIZATION_HASH = "c" * 64
TOKEN = "secret-showcase-access-token"


def prepared(showcase_id: str = "showcase-1") -> PreparedShowcase:
    return PreparedShowcase(
        showcase_id=showcase_id,
        source_master_attempt_id="comment-attempt-1",
        source_post_id="7001",
        source_comment_id="8001",
        source_publication_id="publication-1",
        posting_account="kitascore",
        media_path="C:/staged/showcase.jpg",
        media_sha256=MEDIA_HASH,
        media_size_bytes=123,
        media_public_url=MEDIA_URL,
        media_binding_hash="d" * 64,
        caption_text=(
            "A useful follow-up to @creator's 3D-printing post. "
            "Original: https://www.tiktok.com/@creator/video/7001 "
            "AI-assisted."
        ),
        caption_hash=CAPTION_HASH,
        authorization_hash=AUTHORIZATION_HASH,
        canonical_url="https://www.tiktok.com/@creator/video/7001",
        attempt_number=1,
    )


def claimed(item: PreparedShowcase) -> ClaimedShowcase:
    return ClaimedShowcase(
        showcase_id=item.showcase_id,
        attempt_id=f"local-attempt-{item.showcase_id}",
        attempt_number=item.attempt_number,
        master_attempt_id=f"master-attempt-{item.showcase_id}",
        posting_account=item.posting_account,
        media_path=item.media_path,
        media_sha256=item.media_sha256,
        media_public_url=item.media_public_url,
        caption_text=item.caption_text,
        caption_hash=item.caption_hash,
        authorization_hash=item.authorization_hash,
        canonical_url=item.canonical_url,
    )


class FakeBackend:
    def __init__(
        self,
        events: list,
        *,
        outcome_error: Exception | None = None,
        checkpoint_error: Exception | None = None,
    ):
        self.events = events
        self.outcome_error = outcome_error
        self.checkpoint_error = checkpoint_error
        self.outcomes = []
        self.checkpointed_publish_id = ""

    def prepare(self, showcase_id):
        self.events.append(("prepare", showcase_id))
        return prepared(showcase_id)

    def claim(self, item):
        self.events.append(("claim", item.showcase_id))
        return claimed(item)

    def mark_submit_intent(self, claim):
        self.events.append(("submit_intent", claim.showcase_id))
        return {"status": "submit_intent"}

    def checkpoint_publish_id(self, claim, publish_id):
        self.events.append(("publish_id_checkpoint", publish_id))
        if self.checkpoint_error is not None:
            raise self.checkpoint_error
        self.checkpointed_publish_id = str(publish_id)
        return {
            "status": "submit_intent",
            "publish_id": self.checkpointed_publish_id,
        }

    def record_outcome(self, claim, **kwargs):
        self.events.append(("outcome", kwargs["outcome"]))
        self.outcomes.append(kwargs)
        if self.outcome_error is not None:
            raise self.outcome_error
        status = {
            "published": "published",
            "uncertain": "uncertain",
            "pre_submit_failed": "retryable",
        }[kwargs["outcome"]]
        return {"status": status}


class FakeReconcileBackend(FakeBackend):
    def __init__(self, events, *, state, stored_publish_id=""):
        super().__init__(events)
        self.state = state
        self.stored_publish_id = stored_publish_id

    def active_attempt(self, showcase_id):
        item = prepared(showcase_id)
        return {
            "state": self.state,
            "prepared": item,
            "claim": claimed(item),
            "publish_id": self.stored_publish_id,
            "response": {},
            "error": "",
        }


class FakePreflight:
    def __init__(self, events, *, account="kitascore", error=None):
        self.events = events
        self.account = account
        self.error = error

    async def ensure_ready(self):
        self.events.append(("preflight", self.account))
        if self.error:
            raise self.error
        return {"observed_account": self.account}


class FakeHostedMedia:
    def __init__(self, events, *, error=None, error_on_call=0):
        self.events = events
        self.error = error
        self.error_on_call = int(error_on_call)
        self.calls = 0

    def verify(self, media_url, **kwargs):
        self.calls += 1
        self.events.append(("hosted_media", media_url, kwargs))
        if self.error and (
            not self.error_on_call or self.calls == self.error_on_call
        ):
            raise self.error
        return {
            "status_code": 200,
            "size_bytes": kwargs["expected_size_bytes"],
            "sha256": kwargs["expected_sha256"],
        }


class FakeAPI:
    def __init__(
        self,
        events,
        *,
        initial_error=None,
        initialize_error_stage="",
        status="PUBLISH_COMPLETE",
        public_ids=("9001",),
        fail_reason="",
        downloaded_bytes=123,
    ):
        self.events = events
        self.initial_error = initial_error
        self.initialize_error_stage = initialize_error_stage
        self.status = status
        self.public_ids = public_ids
        self.fail_reason = fail_reason
        self.downloaded_bytes = downloaded_bytes
        self.initialize_kwargs = None

    def query_creator_info(self):
        self.events.append(("creator_info_initial", None))
        if self.initial_error:
            raise self.initial_error
        return SimpleNamespace(creator_username="kitascore")

    def initialize_photo_direct_post(self, **kwargs):
        self.initialize_kwargs = kwargs
        self.events.append(("creator_info_fresh", None))
        if self.initialize_error_stage == "before_callback":
            raise RuntimeError("fresh creator validation failed")
        kwargs["before_submit"]()
        self.events.append(("photo_init", None))
        if self.initialize_error_stage == "after_callback":
            raise RuntimeError(f"submission failed with {TOKEN}")
        return SimpleNamespace(
            publish_id="publish-1",
            creator_username="kitascore",
        )

    def poll_publish_status(self, publish_id, **kwargs):
        self.events.append(("poll", publish_id, kwargs))
        return SimpleNamespace(
            status=self.status,
            publicly_available_post_ids=self.public_ids,
            fail_reason=self.fail_reason,
            downloaded_bytes=self.downloaded_bytes,
        )


class FakeVerifier:
    def __init__(self, events, *, visible=True, persisted=True, error=None):
        self.events = events
        self.visible = visible
        self.persisted = persisted
        self.error = error

    async def verify(self, remote_post_url, **kwargs):
        self.events.append(("remote_verify", remote_post_url, kwargs))
        if self.error:
            raise self.error
        return {
            "visible": self.visible,
            "persisted": self.persisted,
            "verification": "profile7_public_post_twice",
        }


def run_one(
    *,
    backend,
    events,
    api=None,
    hosted=None,
    verifier=None,
    execute=True,
):
    api = api or FakeAPI(events)
    hosted = hosted or FakeHostedMedia(events)
    verifier = verifier or FakeVerifier(events)
    return asyncio.run(
        publish_authorized_showcase(
            backend=backend,
            showcase_id="showcase-1",
            execute=execute,
            access_token=TOKEN if execute else "",
            preflight=FakePreflight(events) if execute else None,
            api_factory=(lambda _token, _account: api) if execute else None,
            hosted_media_checker=hosted if execute else None,
            remote_verifier=verifier if execute else None,
            poll_timeout_seconds=10,
            poll_interval_seconds=0,
            poll_max_attempts=2,
            verified_public_media_base_url=(
                "https://media.example.test/showcases"
            ),
        )
    )


def test_dry_run_does_not_claim_start_browser_or_call_network():
    events = []
    backend = FakeBackend(events)

    result = run_one(backend=backend, events=events, execute=False)

    assert result["mode"] == "dry_run"
    assert result["ready"] is True
    assert result["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert events == [("prepare", "showcase-1")]
    assert backend.outcomes == []


def test_success_orders_all_guards_and_posts_exact_authorized_payload():
    events = []
    backend = FakeBackend(events)
    api = FakeAPI(events)

    result = run_one(backend=backend, events=events, api=api)

    assert result == {
        "mode": "execute",
        "showcase_id": "showcase-1",
        "status": "published",
        "publish_id": "publish-1",
        "remote_post_id": "9001",
        "remote_post_url": "https://www.tiktok.com/@kitascore/photo/9001",
    }
    assert [event[0] for event in events] == [
        "prepare",
        "claim",
        "preflight",
        "creator_info_initial",
        "hosted_media",
        "creator_info_fresh",
        "submit_intent",
        "photo_init",
        "publish_id_checkpoint",
        "poll",
        "hosted_media",
        "remote_verify",
        "outcome",
    ]
    assert api.initialize_kwargs["photo_urls"] == [MEDIA_URL]
    assert api.initialize_kwargs["caption"] == prepared().caption_text
    assert api.initialize_kwargs["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert api.initialize_kwargs["allow_comments"] is True
    assert api.initialize_kwargs["auto_add_music"] is False
    assert backend.outcomes[-1]["outcome"] == "published"
    assert backend.outcomes[-1]["visible"] is True
    assert backend.outcomes[-1]["persisted"] is True
    delivery = backend.outcomes[-1]["response"]["verification"][
        "media_delivery"
    ]
    assert delivery["tiktok_downloaded_bytes"] == 123
    assert delivery["post_complete_hosted_media"]["sha256"] == MEDIA_HASH


def test_publish_id_checkpoint_backend_uses_the_production_signature():
    parameters = inspect.signature(
        SQLiteShowcaseBackend.checkpoint_publish_id
    ).parameters

    assert tuple(parameters) == ("self", "claim", "publish_id")


def test_publish_id_checkpoint_failure_is_uncertain_before_any_status_poll():
    events = []
    backend = FakeBackend(
        events,
        checkpoint_error=RuntimeError("checkpoint storage unavailable"),
    )

    with pytest.raises(
        ShowcasePublishWorkerError,
        match="publish_id_checkpoint failed",
    ):
        run_one(backend=backend, events=events)

    event_names = [item[0] for item in events]
    assert "photo_init" in event_names
    assert "publish_id_checkpoint" in event_names
    assert "poll" not in event_names
    assert backend.outcomes[-1]["outcome"] == "uncertain"
    assert backend.outcomes[-1]["response"]["publish_id"] == "publish-1"
    assert (
        backend.outcomes[-1]["response"]["failed_stage"]
        == "publish_id_checkpoint"
    )


class SimulatedProcessInterruption(BaseException):
    pass


class InterruptOnPollAPI(FakeAPI):
    def poll_publish_status(self, publish_id, **kwargs):
        self.events.append(("poll", publish_id, kwargs))
        raise SimulatedProcessInterruption("process terminated before status poll")


def test_interruption_after_checkpoint_can_reconcile_without_resubmission():
    events = []
    first_backend = FakeBackend(events)

    with pytest.raises(SimulatedProcessInterruption):
        run_one(
            backend=first_backend,
            events=events,
            api=InterruptOnPollAPI(events),
        )

    assert first_backend.checkpointed_publish_id == "publish-1"
    assert first_backend.outcomes == []
    assert [item[0] for item in events][-2:] == [
        "publish_id_checkpoint",
        "poll",
    ]

    recovery_events = []
    recovery_backend = FakeReconcileBackend(
        recovery_events,
        state="submit_intent",
        stored_publish_id=first_backend.checkpointed_publish_id,
    )
    result = asyncio.run(
        reconcile_interrupted_showcase(
            backend=recovery_backend,
            showcase_id="showcase-1",
            execute=True,
            access_token=TOKEN,
            preflight=FakePreflight(recovery_events),
            api_factory=lambda _token, _account: FakeAPI(recovery_events),
            hosted_media_checker=FakeHostedMedia(recovery_events),
            remote_verifier=FakeVerifier(recovery_events),
            poll_timeout_seconds=10,
            poll_interval_seconds=0,
            poll_max_attempts=2,
            verified_public_media_base_url=(
                "https://media.example.test/showcases"
            ),
        )
    )

    assert result["status"] == "published"
    assert "photo_init" not in [item[0] for item in recovery_events]
    assert ("poll", "publish-1", {
        "timeout_seconds": 10,
        "interval_seconds": 0,
        "max_attempts": 2,
    }) in recovery_events


def test_reconcile_releases_only_a_pre_submit_reservation():
    events = []
    backend = FakeReconcileBackend(events, state="reserved")

    result = asyncio.run(
        reconcile_interrupted_showcase(
            backend=backend,
            showcase_id="showcase-1",
            execute=True,
            release_reserved=True,
        )
    )

    assert result["status"] == "retryable"
    assert backend.outcomes[-1]["outcome"] == "pre_submit_failed"


def test_reconcile_verifies_existing_publish_without_resubmitting():
    events = []
    backend = FakeReconcileBackend(
        events,
        state="uncertain",
        stored_publish_id="publish-1",
    )
    api = FakeAPI(events)

    result = asyncio.run(
        reconcile_interrupted_showcase(
            backend=backend,
            showcase_id="showcase-1",
            execute=True,
            access_token=TOKEN,
            preflight=FakePreflight(events),
            api_factory=lambda _token, _account: api,
            hosted_media_checker=FakeHostedMedia(events),
            remote_verifier=FakeVerifier(events),
            poll_timeout_seconds=10,
            poll_interval_seconds=0,
            poll_max_attempts=2,
            verified_public_media_base_url=(
                "https://media.example.test/showcases"
            ),
        )
    )

    assert result["status"] == "published"
    assert [item[0] for item in events] == [
        "preflight",
        "creator_info_initial",
        "hosted_media",
        "poll",
        "hosted_media",
        "remote_verify",
        "outcome",
    ]


@pytest.mark.parametrize(
    ("failure_stage", "expected_event"),
    [
        ("initial_creator", "creator_info_initial"),
        ("hosted_media", "hosted_media"),
        ("fresh_creator", "creator_info_fresh"),
    ],
)
def test_failures_before_submit_intent_are_retryable(
    failure_stage,
    expected_event,
):
    events = []
    backend = FakeBackend(events)
    api = FakeAPI(
        events,
        initial_error=(
            RuntimeError("creator unavailable")
            if failure_stage == "initial_creator"
            else None
        ),
        initialize_error_stage=(
            "before_callback" if failure_stage == "fresh_creator" else ""
        ),
    )
    hosted = FakeHostedMedia(
        events,
        error=(
            RuntimeError("public media unavailable")
            if failure_stage == "hosted_media"
            else None
        ),
    )

    with pytest.raises(ShowcasePublishWorkerError):
        run_one(backend=backend, events=events, api=api, hosted=hosted)

    assert expected_event in [event[0] for event in events]
    assert "submit_intent" not in [event[0] for event in events]
    assert backend.outcomes[-1]["outcome"] == "pre_submit_failed"


def test_any_failure_after_submit_intent_is_uncertain_and_token_safe():
    events = []
    backend = FakeBackend(events)
    api = FakeAPI(events, initialize_error_stage="after_callback")

    with pytest.raises(ShowcasePublishWorkerError) as captured:
        run_one(backend=backend, events=events, api=api)

    assert TOKEN not in str(captured.value)
    assert backend.outcomes[-1]["outcome"] == "uncertain"
    assert TOKEN not in backend.outcomes[-1]["error"]
    assert "submit_intent" in [event[0] for event in events]


@pytest.mark.parametrize(
    ("status", "public_ids"),
    [
        ("PUBLISH_COMPLETE", ()),
        ("PUBLISH_COMPLETE", ("9001", "9002")),
        ("FAILED", ()),
    ],
)
def test_api_must_return_exactly_one_public_post_id(status, public_ids):
    events = []
    backend = FakeBackend(events)

    result = run_one(
        backend=backend,
        events=events,
        api=FakeAPI(events, status=status, public_ids=public_ids),
    )

    assert result["status"] == "uncertain"
    assert result["reason"] == "pending_reconciliation"
    assert backend.outcomes[-1]["outcome"] == "uncertain"
    assert "remote_verify" not in [event[0] for event in events]


def test_publish_complete_downloaded_bytes_must_match_approved_media_size():
    events = []
    backend = FakeBackend(events)

    with pytest.raises(
        ShowcasePublishWorkerError,
        match="downloaded byte count differs",
    ):
        run_one(
            backend=backend,
            events=events,
            api=FakeAPI(events, downloaded_bytes=122),
        )

    assert backend.outcomes[-1]["outcome"] == "uncertain"
    assert backend.outcomes[-1]["response"]["failed_stage"] == (
        "publish_complete_media_revalidation"
    )
    assert [event[0] for event in events].count("hosted_media") == 1
    assert "remote_verify" not in [event[0] for event in events]


def test_publish_complete_rechecks_exact_hosted_bytes_before_confirmation():
    events = []
    backend = FakeBackend(events)
    hosted = FakeHostedMedia(
        events,
        error=RuntimeError("hosted bytes changed after TikTok fetch"),
        error_on_call=2,
    )

    with pytest.raises(
        ShowcasePublishWorkerError,
        match="hosted bytes changed",
    ):
        run_one(
            backend=backend,
            events=events,
            hosted=hosted,
        )

    assert hosted.calls == 2
    assert backend.outcomes[-1]["outcome"] == "uncertain"
    assert backend.outcomes[-1]["response"]["failed_stage"] == (
        "publish_complete_media_revalidation"
    )
    assert "remote_verify" not in [event[0] for event in events]


def test_missing_downloaded_bytes_still_requires_post_complete_hash_check():
    events = []
    backend = FakeBackend(events)
    hosted = FakeHostedMedia(events)

    result = run_one(
        backend=backend,
        events=events,
        api=FakeAPI(events, downloaded_bytes=None),
        hosted=hosted,
    )

    assert result["status"] == "published"
    assert hosted.calls == 2
    delivery = backend.outcomes[-1]["response"]["verification"][
        "media_delivery"
    ]
    assert delivery["tiktok_downloaded_bytes_supplied"] is False
    assert delivery["tiktok_downloaded_bytes"] is None


def test_post_is_uncertain_until_visible_and_persisted_twice_in_profile7():
    events = []
    backend = FakeBackend(events)

    result = run_one(
        backend=backend,
        events=events,
        verifier=FakeVerifier(events, visible=True, persisted=False),
    )

    assert result["status"] == "uncertain"
    assert backend.outcomes[-1]["outcome"] == "uncertain"
    assert backend.outcomes[-1]["visible"] is True
    assert backend.outcomes[-1]["persisted"] is False


class RealVerifierPage:
    def __init__(self, html, *, url=None):
        self.url = url or "https://www.tiktok.com/@kitascore/photo/9001"
        self.html = html

    async def content(self):
        return self.html


def real_visibility_proof(html, *, url=None, status=200):
    return asyncio.run(
        Profile7RemotePostVerifier._visible(
            RealVerifierPage(html, url=url),
            response=SimpleNamespace(status=status),
            account="kitascore",
            post_id="9001",
        )
    )


def test_real_profile7_verifier_accepts_only_exact_canonical_og_url():
    proof = real_visibility_proof(
        '<html><head><meta property="og:url" '
        'content="https://www.tiktok.com/@kitascore/photo/9001">'
        "</head><body>Published photo</body></html>"
    )

    assert proof["visible"] is True
    assert proof["proof_type"] == "exact_canonical_og_url"
    assert proof["observed_og_urls"] == [
        "https://www.tiktok.com/@kitascore/photo/9001"
    ]
    assert proof["http_status"] == 200
    assert proof["observed_url"] == proof["expected_url"]


def test_real_profile7_verifier_accepts_paired_structured_author_and_post():
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.photo-detail": {
                "itemInfo": {
                    "itemStruct": {
                        "id": "9001",
                        "author": {"uniqueId": "kitascore"},
                    }
                }
            }
        }
    }
    proof = real_visibility_proof(
        "<html><head></head><body><script "
        'id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
        'type="application/json">'
        f"{json.dumps(payload)}</script><div>Published photo</div></body></html>"
    )

    assert proof["visible"] is True
    assert proof["proof_type"] == "structured_author_post_identity"
    assert proof["structured_script"] == (
        "__universal_data_for_rehydration__"
    )
    assert proof["structured_identity"]["post_id"] == "9001"
    assert proof["structured_identity"]["account"] == "kitascore"


@pytest.mark.parametrize(
    "html",
    [
        (
            '<html><head><title>Log in | TikTok</title><meta property="og:url" '
            'content="https://www.tiktok.com/@kitascore/photo/9001">'
            "</head><body>Log in to TikTok</body></html>"
        ),
        (
            '<html><head><meta property="og:url" '
            'content="https://www.tiktok.com/@kitascore/photo/9001">'
            '</head><body><div id="captcha-verify-container">'
            "Verify to continue</div></body></html>"
        ),
        (
            '<html><head><meta property="og:url" '
            'content="https://www.tiktok.com/@kitascore/photo/9001">'
            "</head><body>Post is unavailable</body></html>"
        ),
    ],
)
def test_real_profile7_verifier_rejects_login_challenge_and_error_shells(html):
    proof = real_visibility_proof(html)

    assert proof["visible"] is False
    assert proof["rejection_reason"] in {
        "challenge_shell",
        "login_challenge_or_error_shell",
    }


def test_real_profile7_verifier_rejects_unpaired_or_wrong_identity_evidence():
    payload = {
        "items": [
            {"id": "9001", "author": {"uniqueId": "someoneelse"}},
            {"id": "7007", "author": {"uniqueId": "kitascore"}},
        ]
    }
    proof = real_visibility_proof(
        '<html><head><meta property="og:url" '
        'content="https://www.tiktok.com/@someoneelse/photo/9001">'
        '</head><body><script id="SIGI_STATE" type="application/json">'
        f"{json.dumps(payload)}</script><div>Photo</div></body></html>"
    )

    assert proof["visible"] is False
    assert proof["rejection_reason"] == "exact_post_identity_evidence_missing"


def test_real_profile7_verifier_rejects_noncanonical_host_even_with_exact_meta():
    proof = real_visibility_proof(
        '<meta property="og:url" '
        'content="https://www.tiktok.com/@kitascore/photo/9001">',
        url="https://attacker.example/@kitascore/photo/9001",
    )

    assert proof["visible"] is False
    assert proof["rejection_reason"] == "canonical_page_url_mismatch"


def test_batch_has_no_hidden_count_cap():
    events = []
    backend = FakeBackend(events)
    ids = [f"showcase-{index}" for index in range(137)]

    result = asyncio.run(
        publish_showcase_batch(
            backend=backend,
            showcase_ids=ids,
            execute=False,
            verified_public_media_base_url=(
                "https://media.example.test/showcases"
            ),
        )
    )

    assert result["requested"] == 137
    assert result["selected"] == 137
    assert result["processed"] == 137
    assert result["stopped_early"] is False
    assert len([event for event in events if event[0] == "prepare"]) == 137


def test_batch_stops_after_first_uncertain_by_default():
    events = []
    backend = FakeBackend(events)
    api = FakeAPI(events, status="FAILED", public_ids=())

    result = asyncio.run(
        publish_showcase_batch(
            backend=backend,
            showcase_ids=["showcase-1", "showcase-2"],
            execute=True,
            access_token=TOKEN,
            preflight=FakePreflight(events),
            api_factory=lambda _token, _account: api,
            hosted_media_checker=FakeHostedMedia(events),
            remote_verifier=FakeVerifier(events),
            verified_public_media_base_url=(
                "https://media.example.test/showcases"
            ),
        )
    )

    assert result["processed"] == 1
    assert result["uncertain"] == 1
    assert result["stopped_early"] is True
    assert ("prepare", "showcase-2") not in events


def test_verified_public_base_uses_path_boundary_not_string_prefix():
    assert (
        require_url_under_verified_base(
            "https://media.example.test/showcases/one.jpg",
            "https://media.example.test/showcases",
        )
        == "https://media.example.test/showcases/one.jpg"
    )
    with pytest.raises(ShowcasePublishWorkerError):
        require_url_under_verified_base(
            "https://media.example.test/showcases-evil/one.jpg",
            "https://media.example.test/showcases",
        )
    with pytest.raises(ShowcasePublishWorkerError):
        require_url_under_verified_base(
            "https://other.example.test/showcases/one.jpg",
            "https://media.example.test/showcases",
        )
    with pytest.raises(ShowcasePublishWorkerError, match="verified domain"):
        require_url_under_verified_base(
            "https://127.0.0.1/showcases/one.jpg",
            "https://127.0.0.1/showcases",
        )
    with pytest.raises(ShowcasePublishWorkerError, match="public verified domain"):
        require_url_under_verified_base(
            "https://localhost/showcases/one.jpg",
            "https://localhost/showcases",
        )


def test_access_token_requires_an_explicit_memory_only_environment_source():
    assert load_access_token(
        token_env="TIKTOK_TEST_TOKEN",
        environ={"TIKTOK_TEST_TOKEN": TOKEN},
    ) == TOKEN
    with pytest.raises(ShowcasePublishWorkerError):
        load_access_token(
            token_env="",
            environ={},
        )
    with pytest.raises(ShowcasePublishWorkerError):
        load_access_token(
            token_env="TIKTOK_TEST_TOKEN",
            environ={},
        )


class HostedResponse:
    def __init__(self, body, *, status=200, content_type="image/jpeg"):
        self.body = body
        self.status_code = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]

    def close(self):
        self.closed = True


class HostedSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_hosted_media_check_hashes_public_jpeg_without_tiktok_auth():
    body = b"\xff\xd8\xff" + b"public-jpeg-content"
    response = HostedResponse(body)
    session = HostedSession(response)

    result = HostedMediaChecker(session=session).verify(
        MEDIA_URL,
        expected_sha256=hashlib.sha256(body).hexdigest(),
        expected_size_bytes=len(body),
    )

    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert session.calls[0][1]["allow_redirects"] is False
    assert "Authorization" not in session.calls[0][1]["headers"]
    assert response.closed is True


def test_hosted_media_check_rejects_content_binding_change():
    body = b"\xff\xd8\xff" + b"different-content"
    session = HostedSession(HostedResponse(body))

    with pytest.raises(ShowcasePublishWorkerError, match="differs"):
        HostedMediaChecker(session=session).verify(
            MEDIA_URL,
            expected_sha256=MEDIA_HASH,
            expected_size_bytes=len(body),
        )


def test_staging_normalizes_to_api_ready_1080x1920_jpeg(tmp_path):
    capture_root = tmp_path / "captures"
    public_root = tmp_path / "public"
    capture_root.mkdir()
    source = capture_root / "comment.png"
    source.write_bytes(b"raw-screenshot")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"executable-placeholder")
    jpeg_1080x1920 = (
        b"\xff\xd8"
        b"\xff\xc0\x00\x0b\x08\x07\x80\x04\x38\x03\x01\x11\x00"
        b"\xff\xd9"
    )

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(jpeg_1080x1920)
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("publish_comment_showcase.subprocess.run", fake_run):
        result = stage_api_ready_jpeg(
            showcase_id="showcase-1",
            source_path=source,
            expected_source_sha256=source_hash,
            capture_root=capture_root,
            public_directory=public_root,
            public_base_url="https://media.example.test/showcases",
            ffmpeg_path=ffmpeg,
        )

    assert result.width == 1080
    assert result.height == 1920
    assert result.mime_type == "image/jpeg"
    assert result.public_url.startswith(
        "https://media.example.test/showcases/showcase-1-"
    )
    assert Path(result.path).is_file()
