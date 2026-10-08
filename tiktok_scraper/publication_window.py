"""Strict, offline publication-time eligibility for POSTS DISCOVERY.

No post-ID decoding, collection timestamps, clock-relative windows, or timezone
guessing is permitted here. Callers freeze explicit aware bounds once per run.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

UTC = dt.timezone.utc
PUBLICATION_FIELDS = ("published_at", "create_time", "createTime")


def _instant(value: Any, *, unix: bool) -> dt.datetime:
    if isinstance(value, bool) or value is None:
        raise ValueError("publication timestamp must be an aware instant")
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            raise ValueError("publication timestamp is missing")
        numeric = None
        if unix:
            try:
                numeric = Decimal(raw)
            except InvalidOperation:
                pass
        if numeric is not None:
            if not numeric.is_finite() or numeric <= 0 or numeric >= Decimal("253402300800000"):
                raise ValueError("publication UNIX timestamp is invalid")
            # TikTok emits seconds; some saved sources emit milliseconds.
            seconds = numeric / 1000 if numeric >= Decimal("100000000000") else numeric
            try:
                parsed = dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(
                    microseconds=int(seconds * 1_000_000)
                )
            except (OverflowError, ValueError) as exc:
                raise ValueError("publication UNIX timestamp is out of range") from exc
        else:
            if "T" not in raw and " " not in raw:
                raise ValueError("publication timestamp must include time and offset")
            try:
                parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("publication timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("publication timestamp must have an explicit timezone")
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("publication timestamp is out of range") from exc


def _canonical(value: dt.datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def publication_timestamp(value: Any) -> str:
    """Normalize an actual published timestamp; raise ValueError if unknowable."""
    return _canonical(_instant(value, unix=True))


def normalize_publication_window(
    published_after: str = "", published_before: str = ""
) -> dict[str, str]:
    if not published_after and not published_before:
        return {}
    if not published_after or not published_before:
        raise ValueError("both publication-window bounds are required")
    start = _instant(published_after, unix=False)
    end = _instant(published_before, unix=False)
    if start >= end:
        raise ValueError("publication-window start must precede end")
    return {
        "start": _canonical(start),
        "end": _canonical(end),
        "bounds": "[start,end)",
        "order": "published_desc",
    }


def validate_publication_window(window: Mapping[str, Any] | None) -> dict[str, str]:
    if window is None or window == {}:
        return {}
    if not isinstance(window, Mapping) or set(window) != {"start", "end", "bounds", "order"}:
        raise ValueError("publication window must contain its complete frozen contract")
    normalized = normalize_publication_window(window["start"], window["end"])
    if window.get("bounds") != "[start,end)" or window.get("order") != "published_desc":
        raise ValueError("unsupported publication-window bounds or ordering")
    return normalized


def publication_decision(record: Mapping[str, Any], window: Mapping[str, Any]) -> dict[str, Any]:
    """Check actual timestamps; conflicting, missing, invalid, and old are excluded."""
    normalized = validate_publication_window(window)
    if not normalized:
        return {"eligible": True, "reason": "unrestricted", "published_at": ""}
    values = [record[key] for key in PUBLICATION_FIELDS if record.get(key) not in (None, "")]
    if not values:
        return {"eligible": False, "reason": "publication_time_unknown", "published_at": ""}
    try:
        instants = {publication_timestamp(value) for value in values}
    except (ValueError, TypeError, OverflowError):
        return {"eligible": False, "reason": "publication_time_invalid", "published_at": ""}
    if len(instants) != 1:
        return {"eligible": False, "reason": "publication_time_conflict", "published_at": ""}
    published = instants.pop()
    if published < normalized["start"]:
        reason = "published_before_window"
    elif published >= normalized["end"]:
        reason = "published_at_or_after_window_end"
    else:
        reason = "within_window"
    return {"eligible": reason == "within_window", "reason": reason, "published_at": published}
