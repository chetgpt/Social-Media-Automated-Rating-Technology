"""Offline browser-guard tests; no browser or social account is touched."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import engage_browser_guard as guard


class Page:
    def __init__(self, observation=None, frames=()):
        self.observation = {"challenge": False} if observation is None else observation
        self.main_frame = object()
        self.frames = [self.main_frame, *frames]

    async def evaluate(self, script):
        assert script == guard.CHALLENGE_OBSERVATION_JS
        return self.observation


class Frame:
    def __init__(self, *, visible=True, challenge=True, dispose_error=None):
        self.visible, self.challenge = visible, challenge
        self.dispose_error, self.reads, self.disposals = dispose_error, 0, 0

    async def frame_element(self):
        async def visible():
            return self.visible
        async def dispose():
            self.disposals += 1
            if self.dispose_error:
                raise self.dispose_error
        return SimpleNamespace(is_visible=visible, dispose=dispose)

    async def evaluate(self, script):
        self.reads += 1
        return {"challenge": self.challenge, "signal": "challenge_dialog"}


@pytest.mark.parametrize("signal", ["captcha_container", "challenge_dialog", "challenge_frame"])
def test_positive_visible_challenge_has_safe_structured_outcome(signal):
    with pytest.raises(guard.HumanVerificationRequired) as failure:
        asyncio.run(guard.ensure_no_challenge(Page({"challenge": True, "signal": signal}), "before_submit"))
    assert failure.value.as_dict() == {
        "code": "human_verification_required", "phase": "before_submit", "signal": signal}


def test_challenge_metadata_cannot_include_page_text_or_urls():
    failure = guard.HumanVerificationRequired("https://secret/?token=private", "private captcha text")
    assert "private" not in json.dumps(failure.as_dict()) + str(failure)


@pytest.mark.parametrize("observation", [[], True, "false", {}, {"challenge": 0}, {"challenge": "false"}])
def test_malformed_observation_fails_closed(observation):
    with pytest.raises(RuntimeError, match="challenge_observation_unavailable"):
        asyncio.run(guard.ensure_no_challenge(Page(observation)))


def test_hidden_frames_are_never_used_as_challenge_evidence():
    hidden = Frame(visible=False)
    asyncio.run(guard.ensure_no_challenge(Page(frames=[hidden])))
    assert hidden.reads == 0 and hidden.disposals == 1


def test_visible_cross_origin_frame_challenge_is_detected():
    visible = Frame()
    with pytest.raises(guard.HumanVerificationRequired) as failure:
        asyncio.run(guard.ensure_no_challenge(Page(frames=[visible]), "composer"))
    assert failure.value.signal == "challenge_frame"
    assert visible.reads == visible.disposals == 1


def test_handle_disposal_failure_does_not_erase_positive_challenge():
    with pytest.raises(guard.HumanVerificationRequired):
        asyncio.run(guard.ensure_no_challenge(Page(frames=[Frame(dispose_error=RuntimeError("transport"))])))


def test_browser_unavailability_is_distinct_from_challenge():
    class TargetClosedError(RuntimeError):
        pass
    class Disconnected(Page):
        async def evaluate(self, _script):
            raise TargetClosedError("transport private")
    with pytest.raises(TargetClosedError):
        asyncio.run(guard.ensure_no_challenge(Disconnected()))


def test_hung_observation_returns_timeout_without_false_challenge():
    class Hung(Page):
        async def evaluate(self, _script):
            await asyncio.Event().wait()
    with pytest.raises(guard.BrowserOperationTimeout) as failure:
        asyncio.run(guard.ensure_no_challenge(Hung(), timeout=0.01))
    assert failure.value.phase == "challenge_observation"


def test_operation_deadline_does_not_wait_for_suppressed_cancellation():
    async def scenario():
        release = asyncio.Event()
        async def stubborn():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
        started = time.monotonic()
        try:
            with pytest.raises(guard.BrowserOperationTimeout) as failure:
                await guard.bounded_operation(stubborn(), timeout=0.01, cancel_grace=0.01, phase="editor_focus")
            assert time.monotonic() - started < 0.5
            assert failure.value.phase == "editor_focus"
        finally:
            release.set()  # Allow asyncio.run teardown to finish in this test.
            await asyncio.sleep(0)
    asyncio.run(scenario())


def test_success_and_transport_failure_are_not_rewritten():
    async def success():
        return 42
    assert asyncio.run(guard.bounded_operation(success())) == 42
    async def failure():
        raise ValueError("original")
    with pytest.raises(ValueError, match="original"):
        asyncio.run(guard.bounded_operation(failure()))
