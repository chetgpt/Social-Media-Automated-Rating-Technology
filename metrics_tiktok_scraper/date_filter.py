"""Publication-date normalization and auditable campaign window filtering."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATE_FIELDS = (
    "published_at",
    "published",
    "publish_date",
    "published_time",
    "create_time",
    "createTime",
    "taken_at",
    "created_at",
)
DATE_TEXT_FIELDS = ("title", "caption", "description", "desc", "text")
MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "januari": 1,
    "feb": 2,
    "february": 2,
    "februari": 2,
    "mar": 3,
    "march": 3,
    "maret": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mei": 5,
    "jun": 6,
    "june": 6,
    "juni": 6,
    "jul": 7,
    "july": 7,
    "juli": 7,
    "aug": 8,
    "agu": 8,
    "august": 8,
    "agustus": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "okt": 10,
    "october": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "des": 12,
    "december": 12,
    "desember": 12,
}


def _local_timezone() -> dt.tzinfo:
    configured = os.environ.get("SCRAPER_TIMEZONE", "").strip()
    if configured:
        try:
            return ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _build_date(year: int, month: int, day: int, reference: dt.datetime) -> dt.datetime | None:
    try:
        return dt.datetime(year, month, day, tzinfo=reference.tzinfo)
    except ValueError:
        return None


def _absolute_date_from_text(text: str, reference: dt.datetime) -> dt.datetime | None:
    year_first = re.search(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", text)
    if year_first:
        return _build_date(
            int(year_first.group(1)),
            int(year_first.group(2)),
            int(year_first.group(3)),
            reference,
        )

    day_month_year = re.search(r"\b(\d{1,2})\s+([a-z]+)\s*,?\s*(20\d{2})\b", text.lower())
    if day_month_year:
        month = MONTH_NUMBERS.get(day_month_year.group(2))
        if month:
            return _build_date(int(day_month_year.group(3)), month, int(day_month_year.group(1)), reference)

    month_day_year = re.search(r"\b([a-z]+)\s+(\d{1,2})\s*,?\s*(20\d{2})\b", text.lower())
    if month_day_year:
        month = MONTH_NUMBERS.get(month_day_year.group(1))
        if month:
            return _build_date(int(month_day_year.group(3)), month, int(month_day_year.group(2)), reference)

    day_month = re.search(r"\b(\d{1,2})\s+([a-z]{3,9})\b", text.lower())
    if day_month:
        month = MONTH_NUMBERS.get(day_month.group(2))
        if month:
            parsed = _build_date(reference.year, month, int(day_month.group(1)), reference)
            if parsed and parsed > reference + dt.timedelta(days=1):
                parsed = parsed.replace(year=parsed.year - 1)
            return parsed
    return None


def parse_datetime(value: Any, now: dt.datetime | None = None) -> dt.datetime | None:
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if text.isdigit() and int(text) > 1_000_000_000:
        return dt.datetime.fromtimestamp(int(text), tz=dt.timezone.utc)

    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or _local_timezone())
    except ValueError:
        pass

    reference = now or dt.datetime.now(tz=_local_timezone())
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=_local_timezone())
    absolute = _absolute_date_from_text(text, reference)
    if absolute:
        return absolute
    month_day = re.search(r"(?:^|\n)\s*(\d{1,2})-(\d{1,2})\s*(?:$|\n)", text)
    if month_day:
        try:
            parsed = dt.datetime(
                reference.year,
                int(month_day.group(1)),
                int(month_day.group(2)),
                tzinfo=reference.tzinfo,
            )
            if parsed > reference + dt.timedelta(days=1):
                parsed = parsed.replace(year=parsed.year - 1)
            return parsed
        except ValueError:
            pass

    long_relative = re.search(
        r"(\d+)\s*(minute|hour|day|week|month|year|menit|jam|hari|minggu|bulan|tahun)s?\b",
        text.lower(),
    )
    amount = None
    unit = ""
    if long_relative:
        amount = int(long_relative.group(1))
        unit = long_relative.group(2)
    else:
        indonesian_compact = re.search(
            r"\b(\d+)\s*(dtk|mnt|j|jam|h|hr|mg|mgg|bln|thn)\s*lalu\b",
            text.lower(),
        )
        indonesian_units = {
            "dtk": "minute",
            "mnt": "minute",
            "j": "hour",
            "jam": "hour",
            "h": "day",
            "hr": "day",
            "mg": "week",
            "mgg": "week",
            "bln": "month",
            "thn": "year",
        }
        if indonesian_compact:
            amount = int(indonesian_compact.group(1))
            unit = indonesian_units[indonesian_compact.group(2)]

    if amount is None:
        english_compact = re.search(r"\b(\d+)\s*([smhdwy])\s*ago\b", text.lower())
        english_units = {
            "s": "minute",
            "m": "minute",
            "h": "hour",
            "d": "day",
            "w": "week",
            "y": "year",
        }
        if english_compact:
            amount = int(english_compact.group(1))
            unit = english_units[english_compact.group(2)]
    if amount is None:
        return None
    days_by_unit = {
        "minute": 1 / 1440,
        "menit": 1 / 1440,
        "hour": 1 / 24,
        "jam": 1 / 24,
        "day": 1,
        "hari": 1,
        "week": 7,
        "minggu": 7,
        "month": 30,
        "bulan": 30,
        "year": 365,
        "tahun": 365,
    }
    return reference - dt.timedelta(days=amount * days_by_unit[unit])


def candidate_published_at(candidate: dict[str, Any]) -> tuple[str, dt.datetime | None]:
    for field in DATE_FIELDS:
        value = candidate.get(field)
        parsed = parse_datetime(value)
        if parsed:
            return str(value), parsed
    for field in DATE_TEXT_FIELDS:
        value = candidate.get(field)
        parsed = parse_datetime(value)
        if parsed:
            return str(value), parsed
    return "", None


def normalize_windows(windows: Any) -> list[dict[str, Any]]:
    if isinstance(windows, str):
        try:
            windows = json.loads(windows)
        except json.JSONDecodeError:
            windows = []
    if not isinstance(windows, list):
        return []
    output = []
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            continue
        start = parse_datetime(window.get("start"))
        end = parse_datetime(window.get("end"))
        if start or end:
            output.append({
                "name": str(window.get("name") or f"window_{index + 1}"),
                "start": start,
                "end": end,
                "start_raw": str(window.get("start") or ""),
                "end_raw": str(window.get("end") or ""),
            })
    return output


def windows_from_env() -> list[dict[str, Any]]:
    return normalize_windows(os.environ.get("SCRAPER_DATE_WINDOWS_JSON", ""))


def filter_candidates_by_date(
    candidates: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not windows:
        return candidates, []
    kept = []
    audit = []
    for candidate in candidates:
        raw_date, published = candidate_published_at(candidate)
        matched_windows = []
        if published:
            for window in windows:
                start = window.get("start")
                end = window.get("end")
                comparable = published.astimezone(dt.timezone.utc)
                start_utc = start.astimezone(dt.timezone.utc) if start else None
                end_utc = end.astimezone(dt.timezone.utc) if end else None
                if (not start_utc or comparable >= start_utc) and (not end_utc or comparable <= end_utc):
                    matched_windows.append(window["name"])
        decision = "unknown_date" if not published else ("within_date" if matched_windows else "outside_date")
        candidate["_date_filter"] = {
            "decision": decision,
            "published_at": published.isoformat() if published else "",
            "matched_windows": matched_windows,
        }
        if published:
            candidate["published_at"] = published.isoformat()
        audit.append({
            "decision": decision,
            "published_raw": raw_date,
            "published_at": published.isoformat() if published else "",
            "matched_windows": matched_windows,
            "candidate": {
                "video_id": candidate.get("video_id") or candidate.get("id") or candidate.get("media_id"),
                "media_id": candidate.get("media_id"),
                "url": candidate.get("url") or candidate.get("video_url"),
                "title": candidate.get("title") or candidate.get("caption") or "",
                "source": candidate.get("username") or candidate.get("content_creator") or "",
            },
        })
        if decision != "outside_date":
            kept.append(candidate)
    return kept, audit


def write_date_audit(
    folder: str | Path,
    platform: str,
    audit: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> str:
    path = Path(folder) / f"date_{platform}_candidates.json"
    summary = {"within_date": 0, "outside_date": 0, "unknown_date": 0}
    for item in audit:
        summary[item["decision"]] += 1
    payload = {
        "platform": platform,
        "summary": summary,
        "windows": [
            {"name": item["name"], "start": item["start_raw"], "end": item["end_raw"]}
            for item in windows
        ],
        "candidates": audit,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return str(path)
