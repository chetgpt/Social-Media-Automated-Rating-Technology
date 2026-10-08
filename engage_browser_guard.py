"""Bounded browser observations and explicit, human-only challenge handling.

No challenge is clicked, solved, or bypassed here. Transport errors retain
their original types; only a positive visible observation is a challenge.
"""
from __future__ import annotations

import asyncio
import re


def _phase(value: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,80}", value) else "browser_operation"


class HumanVerificationRequired(RuntimeError):
    code = "human_verification_required"

    def __init__(self, phase="browser_operation", signal="visible_challenge"):
        self.phase = _phase(phase)
        self.signal = signal if signal in {"captcha_container", "challenge_dialog", "challenge_frame", "visible_challenge"} else "visible_challenge"
        super().__init__(f"{self.code}: {self.phase}")

    def as_dict(self):
        return {"code": self.code, "phase": self.phase, "signal": self.signal}


class BrowserOperationTimeout(TimeoutError):
    code = "browser_operation_timeout"

    def __init__(self, phase="browser_operation", timeout=10.0):
        self.phase = _phase(phase)
        self.timeout_seconds = float(timeout)
        super().__init__(f"{self.code}: {self.phase}")

    def as_dict(self):
        return {"code": self.code, "phase": self.phase, "timeout_seconds": self.timeout_seconds}


def _consume_completion(task):
    if not task.cancelled():
        task.exception()


async def bounded_operation(awaitable, timeout=10.0, phase="browser_operation", *, cancel_grace=0.25):
    """Return within the deadline plus a bounded cancellation grace.

    asyncio.wait_for can wait forever when a transport suppresses cancellation.
    A timed-out operation must terminate the caller's workflow; callers must
    never continue composing/submitting on the same page after this exception.
    A process supervisor remains the backstop for an unresponsive transport.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    except BaseException:
        task.cancel()
        task.add_done_callback(_consume_completion)
        raise
    if done:
        return task.result()
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=max(0.0, cancel_grace))
    finally:
        task.add_done_callback(_consume_completion)
    raise BrowserOperationTimeout(phase, timeout)


CHALLENGE_OBSERVATION_JS = r"""() => {
    const visible = el => {
        if (!el || !el.isConnected) return false;
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        for (let ancestor = el; ancestor; ancestor = ancestor.parentElement) {
            const style = getComputedStyle(ancestor);
            if (style.display === 'none' || style.visibility === 'hidden' ||
                style.visibility === 'collapse' || Number(style.opacity) === 0) return false;
        }
        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
            r.top < innerHeight && r.left < innerWidth && s.display !== 'none' &&
            s.visibility !== 'hidden' && s.visibility !== 'collapse' && Number(s.opacity) !== 0;
    };
    const selectors = '#captcha-verify-container, #captcha_container, #captcha-container, ' +
        '[data-e2e="captcha"], [class*="captcha_verify_container"], [class*="CaptchaModal"], ' +
        '[id^="tiktok-verify"]';
    if (Array.from(document.querySelectorAll(selectors)).some(visible))
        return {challenge: true, signal: 'captcha_container'};
    const text = /drag the slider to fit the puzzle|verify (?:that )?you(?: are|'re) human|complete (?:the|this) (?:security )?(?:check|challenge)|select (?:all )?(?:the )?matching (?:objects|shapes)|geser.*(?:puzzle|teka.teki)|verifikasi.*(?:manusia|keamanan)/i;
    const dialogs = document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="captcha"], [class*="Captcha"], [id*="verify"]');
    if (Array.from(dialogs).slice(0, 100).some(el => visible(el) && text.test((el.innerText || '').slice(0, 4000))))
        return {challenge: true, signal: 'challenge_dialog'};
    for (const frame of Array.from(document.querySelectorAll('iframe')).slice(0, 20)) {
        if (!visible(frame)) continue;
        const marker = [frame.getAttribute('src'), frame.getAttribute('title'), frame.id, frame.name].join(' ');
        if (/(?:captcha|verifycenter|verify-center|security.?challenge|human.?verification)/i.test(marker))
            return {challenge: true, signal: 'challenge_frame'};
    }
    return {challenge: false};
}"""


def _checked_observation(observation):
    if not isinstance(observation, dict) or type(observation.get("challenge")) is not bool:
        raise RuntimeError("challenge_observation_unavailable")
    return observation


async def _observe_challenge(page):
    observation = _checked_observation(await page.evaluate(CHALLENGE_OBSERVATION_JS))
    if observation["challenge"]:
        return observation
    # A cross-origin frame can contain the challenge without identifying it in
    # its URL. Check only a bounded number of frames whose element is visible.
    for frame in list(page.frames)[:9]:
        if frame == page.main_frame:
            continue
        element = await frame.frame_element()
        observation = {"challenge": False}
        primary_error = None
        try:
            if await element.is_visible():
                observation = _checked_observation(await frame.evaluate(CHALLENGE_OBSERVATION_JS))
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await bounded_operation(element.dispose(), timeout=0.5, phase="challenge_frame_dispose")
            except Exception:
                if primary_error is None and not observation["challenge"]:
                    raise
        if observation["challenge"]:
            return {"challenge": True, "signal": "challenge_frame"}
    return {"challenge": False}


async def ensure_no_challenge(page, phase="browser_operation", timeout=3.0):
    observation = await bounded_operation(_observe_challenge(page), timeout=timeout, phase="challenge_observation")
    if observation.get("challenge") is True:
        raise HumanVerificationRequired(phase, observation.get("signal"))
