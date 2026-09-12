"""Read-only link discovery for TikTok One's Top Content page.

This adapter deliberately has a much narrower evidence surface than the
canonical TikTok collector.  It uses the already-authorized Edge Profile 7
session only to enumerate canonical TikTok video URLs.  It does not retain
page HTML, response payloads, request URLs, cookies, media, captions, comments,
or creator images, and it has no outbound action surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import re
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TOP_CONTENT_PATH = "/creative/forpartners/creator/top-content"
TOP_CONTENT_HOST = "ads.tiktok.com"
TOP_CONTENT_CARD_LIMIT = 100
TOP_CONTENT_CARD_SELECTOR = '[data-testid="TopContentVideoCard-index-mcG3qs"]'
TOP_CONTENT_LINK_SELECTOR = (
    '[data-testid="CreatorVideoPlayerModal-CreatorVideoPlayer-rMC8wC"]'
)
TOP_CONTENT_PAGINATION_SELECTOR = (
    '[data-testid="CreatorInfoPaginationModal-ModalPaginationButton-kzKSry"]'
)
TOP_CONTENT_CLOSE_SELECTOR = (
    '[data-testid="CreatorVideoPlayerModal-ModalCloseButton-3kWjH5"]'
)

RANKING_LABELS: dict[str, str] = {
    "video_views": "Highest video views",
    "engagement": "Highest engagement",
    "six_second_views": "Highest 6s views",
}
DEFAULT_RANKINGS = tuple(RANKING_LABELS)

_REGION_PATTERN = re.compile(r"^[a-z]{2,8}$")
_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,64}$")
_VIDEO_PATH_PATTERN = re.compile(
    r"^/@(?P<creator>[A-Za-z0-9._]{1,64})/video/(?P<video_id>\d+)/?$"
)
_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,64}$")


class TopContentBrowserError(RuntimeError):
    """The Top Content browser source failed closed."""


def normalize_top_content_source_url(value: str) -> tuple[str, str]:
    """Validate and normalize the one supported TikTok One page URL."""

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        invalid_port = parsed.port not in (None, 443)
    except ValueError as exc:
        raise TopContentBrowserError("invalid Top Content source URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != TOP_CONTENT_HOST
        or parsed.username is not None
        or parsed.password is not None
        or invalid_port
        or parsed.path.rstrip("/") != TOP_CONTENT_PATH
        or parsed.fragment
    ):
        raise TopContentBrowserError(
            "Top Content source must use the exact ads.tiktok.com Top Content page"
        )
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) > 1:
        raise TopContentBrowserError(
            "Top Content source query must contain only one region value"
        )
    region = "row"
    if query:
        key, raw_region = query[0]
        if key.strip().casefold() != "region":
            raise TopContentBrowserError(
                "Top Content source query must contain only one region value"
            )
        region = raw_region.strip().casefold()
    if not _REGION_PATTERN.fullmatch(region):
        raise TopContentBrowserError("Top Content region is invalid")
    normalized = urlunsplit(
        ("https", TOP_CONTENT_HOST, TOP_CONTENT_PATH, urlencode({"region": region}), "")
    )
    return normalized, region


def normalize_tiktok_video_url(value: Any) -> dict[str, str] | None:
    """Return a safe canonical TikTok video identity or ``None``."""

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in {"www.tiktok.com", "tiktok.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed_port is not None
    ):
        return None
    match = _VIDEO_PATH_PATTERN.fullmatch(parsed.path)
    if match is None:
        return None
    creator = match.group("creator")
    video_id = match.group("video_id")
    if not _HANDLE_PATTERN.fullmatch(creator):
        return None
    return {
        "video_id": video_id,
        "creator": creator,
        "url": f"https://www.tiktok.com/@{creator}/video/{video_id}",
    }


def _normalize_rankings(rankings: Sequence[str]) -> tuple[str, ...]:
    if isinstance(rankings, (str, bytes)):
        raise TopContentBrowserError("rankings must be a sequence")
    values: list[str] = []
    for raw in rankings:
        value = str(raw or "").strip()
        if value not in RANKING_LABELS:
            raise TopContentBrowserError(f"unsupported Top Content ranking: {value}")
        if value not in values:
            values.append(value)
    if not values:
        raise TopContentBrowserError("at least one Top Content ranking is required")
    return tuple(values)


def _normalize_exclusions(values: Collection[str]) -> set[str]:
    if isinstance(values, (str, bytes)):
        raise TopContentBrowserError("exclude_video_ids must be a collection")
    normalized: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value.isdigit():
            raise TopContentBrowserError("exclude_video_ids contains an invalid ID")
        normalized.add(value)
    return normalized


def _safe_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _sanitize_filter_snapshot(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    tags = raw.get("selected_content_tags")
    if not isinstance(tags, list):
        tags = []
    safe_tags: list[str] = []
    for item in tags[:50]:
        safe = _safe_text(item, 100)
        if safe and safe not in safe_tags:
            safe_tags.append(safe)
    organic = raw.get("organic_only")
    return {
        "country": _safe_text(raw.get("country"), 100),
        "period": _safe_text(raw.get("period"), 100),
        "selected_content_tags": safe_tags,
        "organic_only": organic if isinstance(organic, bool) else None,
        "last_updated_label": _safe_text(raw.get("last_updated_label"), 200),
    }


async def _read_filter_snapshot(page: Any) -> dict[str, Any]:
    try:
        value = await page.evaluate(
            r"""
            () => { // TOP_CONTENT_FILTER_SNAPSHOT
              const visible = (node) => {
                if (!node) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const text = (node) => String(node?.textContent || '').replace(/\s+/g, ' ').trim();
              const controls = [...document.querySelectorAll('input, button, [role="combobox"], [role="option"], [role="checkbox"]')]
                .filter(visible);
              const controlText = (node) => [
                node.getAttribute?.('aria-label') || '',
                node.getAttribute?.('placeholder') || '',
                node.value || '',
                text(node),
              ].join(' ').trim();
              const findValue = (needle) => {
                const found = controls.find((node) => controlText(node).toLowerCase().includes(needle));
                if (!found) return '';
                return String(found.value || found.getAttribute?.('aria-valuetext') || text(found) || '').trim();
              };
              const tags = [...document.querySelectorAll('[aria-selected="true"], [data-selected="true"], input:checked')]
                .filter(visible)
                .map((node) => text(node.closest('label, button, [role="option"]') || node))
                .filter(Boolean);
              const organicNode = controls.find((node) => controlText(node).toLowerCase().includes('organic'));
              let organicOnly = null;
              if (organicNode) {
                organicOnly = organicNode.checked === true
                  || organicNode.getAttribute?.('aria-checked') === 'true';
              }
              const lastUpdatedNode = [...document.querySelectorAll('body *')]
                .filter(visible)
                .find((node) => /^last updated\b/i.test(text(node)) && node.children.length < 3);
              return {
                country: findValue('country'),
                period: findValue('period'),
                selected_content_tags: tags,
                organic_only: organicOnly,
                last_updated_label: text(lastUpdatedNode),
              };
            }
            """
        )
    except Exception:
        value = {}
    return _sanitize_filter_snapshot(value)


async def _wait(page: Any, milliseconds: int) -> None:
    try:
        await page.wait_for_timeout(milliseconds)
    except Exception:
        await asyncio.sleep(max(0, milliseconds) / 1000)


async def _visible_text_locator(page: Any, label: str) -> Any | None:
    try:
        locator = page.get_by_text(label, exact=True)
        count = min(await locator.count(), 10)
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
    except Exception:
        return None
    return None


async def _select_ranking(page: Any, ranking: str) -> None:
    """Select only one of the three known read-only ranking controls."""

    label = RANKING_LABELS[ranking]
    visible: dict[str, Any] = {}
    for key, candidate_label in RANKING_LABELS.items():
        locator = await _visible_text_locator(page, candidate_label)
        if locator is not None:
            visible[key] = locator
    if ranking in visible and len(visible) == 1:
        return
    current_key = next((key for key in RANKING_LABELS if key in visible), "")
    if current_key == ranking:
        return
    if current_key:
        await visible[current_key].click()
    else:
        try:
            opener = page.locator(
                '[role="combobox"], button[aria-haspopup="listbox"], '
                '[aria-haspopup="listbox"]'
            )
            if await opener.count() < 1:
                raise TopContentBrowserError(
                    "Top Content ranking control is unavailable"
                )
            await opener.first.click()
        except TopContentBrowserError:
            raise
        except Exception as exc:
            raise TopContentBrowserError(
                "Top Content ranking control is unavailable"
            ) from exc
    await _wait(page, 150)
    target = await _visible_text_locator(page, label)
    if target is None:
        raise TopContentBrowserError(
            f"Top Content ranking option is unavailable: {ranking}"
        )
    await target.click()
    await _wait(page, 800)


async def _card_count(page: Any) -> int:
    try:
        return max(0, int(await page.locator(TOP_CONTENT_CARD_SELECTOR).count()))
    except Exception:
        return 0


async def _load_ranking_cards(page: Any) -> tuple[int, bool, str]:
    """Load the observed Top 100; a stable sub-100 stall is not a frontier."""

    await _wait(page, 800)
    count = min(TOP_CONTENT_CARD_LIMIT, await _card_count(page))
    stable_attempts = 0
    attempts = 0
    while count < TOP_CONTENT_CARD_LIMIT and attempts < 40:
        attempts += 1
        previous = count
        try:
            await page.evaluate(
                """
                () => { // TOP_CONTENT_SCROLL
                  window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
                }
                """
            )
        except Exception:
            pass
        await _wait(page, 700)
        count = min(TOP_CONTENT_CARD_LIMIT, await _card_count(page))
        if count > previous:
            stable_attempts = 0
        else:
            stable_attempts += 1
        if stable_attempts >= 3:
            return count, False, "stable_card_stall_below_100"
    if count >= TOP_CONTENT_CARD_LIMIT:
        return TOP_CONTENT_CARD_LIMIT, True, ""
    return count, False, "card_load_attempt_limit_below_100"


async def _right_pagination_button(page: Any) -> Any | None:
    try:
        buttons = page.locator(TOP_CONTENT_PAGINATION_SELECTOR)
        count = min(await buttons.count(), 8)
    except Exception:
        return None
    fallback = None
    for index in range(count):
        button = buttons.nth(index)
        fallback = button
        signals: list[str] = []
        for name in ("aria-label", "title", "data-direction", "class"):
            try:
                signals.append(str(await button.get_attribute(name) or ""))
            except Exception:
                pass
        if any(token in " ".join(signals).casefold() for token in ("right", "next")):
            return button
        try:
            right_icon = button.locator(
                '[aria-label*="right" i], [aria-label*="next" i], '
                '[data-icon*="right" i], [class*="right" i], [class*="next" i]'
            )
            if await right_icon.count():
                return button
        except Exception:
            pass
    return fallback if count == 1 else (buttons.nth(count - 1) if count else None)


async def _modal_href(page: Any) -> str:
    try:
        links = page.locator(TOP_CONTENT_LINK_SELECTOR)
        if await links.count() < 1:
            return ""
        return str(await links.first.get_attribute("href") or "").strip()
    except Exception:
        return ""


async def _wait_for_modal_href_change(page: Any, previous: str) -> str:
    for _ in range(20):
        value = await _modal_href(page)
        if value and value != previous:
            return value
        await _wait(page, 100)
    return ""


async def _close_modal(page: Any) -> None:
    try:
        controls = page.locator(TOP_CONTENT_CLOSE_SELECTOR)
        if await controls.count():
            await controls.first.click()
    except Exception:
        pass


async def _traverse_loaded_ranking(
    page: Any,
    *,
    ranking: str,
    loaded_count: int,
    requested_count: int,
    links: list[dict[str, Any]],
    seen_ids: set[str],
    excluded_ids: set[str],
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "ranking": ranking,
        "ranking_label": RANKING_LABELS[ranking],
        "loaded_cards": loaded_count,
        "traversed_cards": 0,
        "accepted_links": 0,
        "duplicate_links": 0,
        "excluded_video_ids": 0,
        "invalid_links": 0,
        "modal_frontier_reached": False,
        "reason": "",
    }
    if loaded_count <= 0:
        stats["reason"] = "no_top_content_cards"
        return stats
    cards = page.locator(TOP_CONTENT_CARD_SELECTOR)
    try:
        if await cards.count() < 1:
            stats["reason"] = "top_content_card_control_unavailable"
            return stats
        await cards.first.click()
        await _wait(page, 300)
    except Exception:
        stats["reason"] = "top_content_modal_open_failed"
        return stats
    try:
        href = await _modal_href(page)
        for position in range(1, loaded_count + 1):
            if not href:
                stats["reason"] = "top_content_modal_link_unavailable"
                break
            stats["traversed_cards"] += 1
            normalized = normalize_tiktok_video_url(href)
            if normalized is None:
                stats["invalid_links"] += 1
            elif normalized["video_id"] in excluded_ids:
                stats["excluded_video_ids"] += 1
            elif normalized["video_id"] in seen_ids:
                stats["duplicate_links"] += 1
            else:
                seen_ids.add(normalized["video_id"])
                links.append(
                    {
                        "ordinal": len(links) + 1,
                        "video_id": normalized["video_id"],
                        "url": normalized["url"],
                        "creator": normalized["creator"],
                        "ranking": ranking,
                        "ranking_position": position,
                    }
                )
                stats["accepted_links"] += 1
                if len(links) >= requested_count:
                    break
            if position >= loaded_count:
                stats["modal_frontier_reached"] = True
                break
            next_button = await _right_pagination_button(page)
            if next_button is None:
                stats["reason"] = "top_content_next_control_unavailable"
                break
            previous = href
            try:
                disabled = str(await next_button.get_attribute("disabled") or "")
                aria_disabled = str(
                    await next_button.get_attribute("aria-disabled") or ""
                )
                if disabled or aria_disabled.casefold() == "true":
                    stats["reason"] = "top_content_next_control_disabled_early"
                    break
                await next_button.click()
            except Exception:
                stats["reason"] = "top_content_next_control_failed"
                break
            href = await _wait_for_modal_href_change(page, previous)
            if not href:
                stats["reason"] = "top_content_modal_pagination_stalled"
                break
        else:
            stats["modal_frontier_reached"] = True
        if stats["traversed_cards"] == loaded_count:
            stats["modal_frontier_reached"] = True
    finally:
        await _close_modal(page)
    return stats


async def _discover_from_page(
    page: Any,
    *,
    normalized_source_url: str,
    region: str,
    requested_count: int,
    rankings: tuple[str, ...],
    excluded_ids: set[str],
    account_binding_hash: str,
) -> dict[str, Any]:
    filter_snapshot = await _read_filter_snapshot(page)
    links: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    ranking_rows: list[dict[str, Any]] = []
    exhausted: list[str] = []
    reasons: list[str] = []

    for ranking in rankings:
        if len(links) >= requested_count:
            break
        try:
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        await _select_ranking(page, ranking)
        loaded_count, card_frontier, load_reason = await _load_ranking_cards(page)
        row = await _traverse_loaded_ranking(
            page,
            ranking=ranking,
            loaded_count=loaded_count,
            requested_count=requested_count,
            links=links,
            seen_ids=seen_ids,
            excluded_ids=excluded_ids,
        )
        row["card_frontier_verified"] = card_frontier
        if load_reason and not row["reason"]:
            row["reason"] = load_reason
        ranking_exhausted = bool(
            card_frontier
            and row["modal_frontier_reached"]
            and row["traversed_cards"] == TOP_CONTENT_CARD_LIMIT
        )
        row["ranking_exhausted"] = ranking_exhausted
        if ranking_exhausted:
            exhausted.append(ranking)
        elif len(links) < requested_count:
            reasons.append(f"{ranking}:{row['reason'] or 'frontier_unverified'}")
        ranking_rows.append(row)

    excluded_count = sum(int(row["excluded_video_ids"]) for row in ranking_rows)
    duplicate_count = sum(int(row["duplicate_links"]) for row in ranking_rows)
    if excluded_count:
        reasons.append(f"excluded_video_ids:{excluded_count}")
    if duplicate_count:
        reasons.append(f"duplicate_video_ids:{duplicate_count}")
    fulfilled = len(links) == requested_count
    all_rankings_exhausted = len(exhausted) == len(rankings)
    if not fulfilled:
        reasons.append(
            "source_exhausted_before_requested_count"
            if all_rankings_exhausted
            else "unverified_frontier_before_requested_count"
        )
    frontier_verified = fulfilled or all_rankings_exhausted
    return {
        "source_url": normalized_source_url,
        "region": region,
        "filter_snapshot": filter_snapshot,
        "list_snapshot": {
            "requested_count": requested_count,
            "rankings_requested": list(rankings),
            "ranking_observations": ranking_rows,
            "unique_links_found": len(links),
            "excluded_video_ids": excluded_count,
            "duplicate_video_ids": duplicate_count,
        },
        "account_binding_hash": account_binding_hash,
        "links": links,
        "rankings_exhausted": exhausted,
        "frontier_verified": frontier_verified,
        "reasons": reasons,
    }


async def discover_top_content_links(
    source_url: str,
    requested_count: int,
    rankings: Sequence[str] = DEFAULT_RANKINGS,
    social_browser_runtime_dir: str | Path | None = None,
    startup_timeout: float = 120.0,
    expected_account: str = "",
    exclude_video_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Collect an exact bounded list of canonical links from Top Content.

    The function starts or reuses only the designated Edge Profile 7, opens one
    temporary tab, and disconnects without closing the browser or its context.
    Existing video IDs may be excluded so a durable caller can request a
    globally-new list against the canonical TikTok master registry.
    """

    if isinstance(requested_count, bool) or not isinstance(requested_count, int):
        raise TopContentBrowserError("requested_count must be a positive integer")
    if requested_count <= 0:
        raise TopContentBrowserError("requested_count must be a positive integer")
    normalized_source_url, region = normalize_top_content_source_url(source_url)
    normalized_rankings = _normalize_rankings(rankings)
    excluded_ids = _normalize_exclusions(exclude_video_ids)
    expected = str(expected_account or "").strip().lstrip("@").casefold()
    if expected and not _ACCOUNT_PATTERN.fullmatch(expected):
        raise TopContentBrowserError("expected_account is invalid")

    try:
        from playwright.async_api import async_playwright
        from engage_tiktok import active_tiktok_account
        from social_browser import (
            DEFAULT_RUNTIME_DIR,
            ensure_engage_profile7_browser,
            load_verified_profile7_state,
            platform_authentication,
            verified_profile_context,
        )
    except Exception as exc:
        raise TopContentBrowserError(
            f"Profile 7 browser tooling is unavailable: {type(exc).__name__}"
        ) from exc

    runtime_dir = Path(
        social_browser_runtime_dir
        if social_browser_runtime_dir is not None
        else DEFAULT_RUNTIME_DIR
    ).resolve()
    try:

        def quiet_profile7_start() -> Any:
            # The shared launcher prints platform cookie *names* for interactive
            # diagnostics. Link discovery needs a JSON-only, minimized output
            # surface, so keep that preflight chatter out of this command.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                return ensure_engage_profile7_browser(
                    runtime_dir,
                    startup_timeout=max(30.0, float(startup_timeout)),
                    open_tabs=False,
                )

        session = await asyncio.to_thread(
            quiet_profile7_start,
        )
        designation = session.get("designation") if isinstance(session, dict) else None
        if not isinstance(designation, dict):
            raise TopContentBrowserError("Profile 7 designation is unavailable")
        state = load_verified_profile7_state(runtime_dir, designation)
        cdp_url = str(state.get("cdp_url") or "")
        if not cdp_url:
            raise TopContentBrowserError("Profile 7 connection is unavailable")
    except TopContentBrowserError:
        raise
    except Exception as exc:
        raise TopContentBrowserError(
            f"Profile 7 startup or verification failed: {type(exc).__name__}"
        ) from exc

    page: Any = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context, _identity = await verified_profile_context(browser, designation)
            authentication = await platform_authentication(context, "tiktok")
            if authentication.get("authenticated") is not True:
                raise TopContentBrowserError("Profile 7 is not authenticated to TikTok")
            page = await context.new_page()
            try:

                async def block_heavy_resources(route: Any) -> None:
                    try:
                        resource_type = str(
                            route.request.resource_type or ""
                        ).casefold()
                        if resource_type in {"media", "image", "font"}:
                            await route.abort()
                        else:
                            await route.continue_()
                    except Exception:
                        try:
                            await route.abort()
                        except Exception:
                            pass

                await page.route("**/*", block_heavy_resources)
                timeout_ms = max(30_000, int(float(startup_timeout) * 1000))
                with contextlib.suppress(Exception):
                    page.set_default_timeout(min(timeout_ms, 15_000))
                    page.set_default_navigation_timeout(min(timeout_ms, 60_000))
                try:
                    await page.goto(
                        "https://www.tiktok.com/",
                        wait_until="domcontentloaded",
                        timeout=min(timeout_ms, 60_000),
                    )
                except Exception as exc:
                    raise TopContentBrowserError(
                        f"TikTok account page navigation failed: {type(exc).__name__}"
                    ) from exc
                try:
                    observed_account = (
                        str(
                            await active_tiktok_account(
                                page,
                                timeout_ms=min(timeout_ms, 20_000),
                            )
                            or ""
                        )
                        .strip()
                        .lstrip("@")
                        .casefold()
                    )
                except Exception as exc:
                    raise TopContentBrowserError(
                        f"TikTok account binding failed: {type(exc).__name__}"
                    ) from exc
                if not observed_account or not _ACCOUNT_PATTERN.fullmatch(
                    observed_account
                ):
                    raise TopContentBrowserError(
                        "Profile 7 TikTok account binding could not be resolved"
                    )
                if expected and observed_account != expected:
                    raise TopContentBrowserError(
                        "Profile 7 TikTok account does not match"
                    )
                account_binding_hash = hashlib.sha256(
                    f"tiktok-top-content-account-v1\0{observed_account}".encode("utf-8")
                ).hexdigest()
                try:
                    await page.goto(
                        normalized_source_url,
                        wait_until="commit",
                        timeout=min(timeout_ms, 60_000),
                    )
                    load_state = getattr(page, "wait_for_load_state", None)
                    if callable(load_state):
                        with contextlib.suppress(Exception):
                            await load_state("domcontentloaded", timeout=30_000)
                except Exception as exc:
                    raise TopContentBrowserError(
                        f"Top Content page navigation failed: {type(exc).__name__}"
                    ) from exc
                await _wait(page, 1_000)
                try:
                    return await _discover_from_page(
                        page,
                        normalized_source_url=normalized_source_url,
                        region=region,
                        requested_count=requested_count,
                        rankings=normalized_rankings,
                        excluded_ids=excluded_ids,
                        account_binding_hash=account_binding_hash,
                    )
                except TopContentBrowserError:
                    raise
                except Exception as exc:
                    raise TopContentBrowserError(
                        f"Top Content page traversal failed: {type(exc).__name__}"
                    ) from exc
            finally:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
    except TopContentBrowserError:
        raise
    except Exception as exc:
        raise TopContentBrowserError(
            f"Top Content link discovery failed: {type(exc).__name__}"
        ) from exc
    finally:
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass


__all__ = [
    "DEFAULT_RANKINGS",
    "RANKING_LABELS",
    "TopContentBrowserError",
    "discover_top_content_links",
    "normalize_tiktok_video_url",
    "normalize_top_content_source_url",
]
