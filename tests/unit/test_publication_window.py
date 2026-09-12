import datetime as dt

import pytest

from tiktok_scraper.publication_window import (
    normalize_publication_window, publication_decision, publication_timestamp,
    validate_publication_window,
)


WINDOW = normalize_publication_window("2026-08-24T00:00:00Z", "2026-08-31T00:00:00Z")


def test_absent_window_preserves_legacy_no_timestamp_requirement():
    assert normalize_publication_window() == {}
    assert publication_decision({}, {})["eligible"] is True


def test_window_normalizes_offsets_and_uses_half_open_bounds():
    assert normalize_publication_window("2026-08-24T07:00:00+07:00", "2026-08-31T07:00:00+07:00") == WINDOW
    assert WINDOW == {"start": "2026-08-24T00:00:00.000000Z", "end": "2026-08-31T00:00:00.000000Z", "bounds": "[start,end)", "order": "published_desc"}
    assert publication_decision({"published_at": WINDOW["start"]}, WINDOW)["eligible"]
    assert not publication_decision({"published_at": WINDOW["end"]}, WINDOW)["eligible"]


@pytest.mark.parametrize("start,end", [
    ("2026-08-24", "2026-08-31"),
    ("2026-08-24T00:00:00", "2026-08-31T00:00:00Z"),
    ("2026-08-24T00:00:00Z", ""),
    ("", "2026-08-31T00:00:00Z"),
    (WINDOW["end"], WINDOW["start"]),
    (WINDOW["start"], WINDOW["start"]),
    ("1", "2"),
])
def test_invalid_bounds_fail_closed(start, end):
    with pytest.raises(ValueError):
        normalize_publication_window(start, end)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, "NaN", "Infinity", "1e999999", "2026-08-28", "2026-08-28T00:00:00", "tomorrow", {}, []])
def test_invalid_publication_timestamp(value):
    with pytest.raises((ValueError, TypeError)):
        publication_timestamp(value)


@pytest.mark.parametrize("field", ["published_at", "create_time", "createTime"])
@pytest.mark.parametrize("as_milliseconds", [False, True])
def test_numeric_publication_seconds_and_milliseconds(field, as_milliseconds):
    when = dt.datetime(2026, 8, 29, 3, 4, 5, tzinfo=dt.timezone.utc)
    timestamp = int(when.timestamp()) * (1000 if as_milliseconds else 1)
    decision = publication_decision({field: str(timestamp)}, WINDOW)
    assert decision == {"eligible": True, "reason": "within_window", "published_at": "2026-08-29T03:04:05.000000Z"}


def test_conflicting_actual_dates_are_not_arbitrarily_chosen():
    assert publication_decision({"published_at": WINDOW["start"], "create_time": WINDOW["end"]}, WINDOW)["reason"] == "publication_time_conflict"


def test_equivalent_timestamp_fields_are_not_conflicting():
    assert publication_decision({"published_at": "2026-08-24T07:00:00+07:00", "create_time": WINDOW["start"]}, WINDOW)["eligible"]


def test_missing_timestamp_never_uses_post_id_or_collection_date():
    result = publication_decision({"post_id": "9999999999999999999", "collected_at": WINDOW["start"], "observed_at": WINDOW["start"]}, WINDOW)
    assert result["reason"] == "publication_time_unknown"


def test_fractional_boundaries_sort_lexically_without_precision_loss():
    values = ["2026-08-25T00:00:00Z", "2026-08-25T00:00:00.000001Z"]
    assert sorted(map(publication_timestamp, values), reverse=True)[0].endswith(".000001Z")


@pytest.mark.parametrize("window", [[], "window", {"start": WINDOW["start"]}, {**WINDOW, "bounds": "[]"}, {**WINDOW, "order": "views_desc"}, {**WINDOW, "other": 1}])
def test_frozen_window_shape_is_complete_and_strict(window):
    with pytest.raises(ValueError):
        validate_publication_window(window)
