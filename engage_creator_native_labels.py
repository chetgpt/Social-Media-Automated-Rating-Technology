"""Bind observed native labels to same-run creator choices, entirely offline.

A successful probe is composition evidence, never approval or publication
authority. The publication adapter verifies the current native entities again.
This helper changes only a deep copy of the supplied matching records.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

import engage_creator_matching as matching


def _handle(value: object, *, normalized: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("native probe requires a valid exact account handle")
    handle = value.removeprefix("@").casefold()
    if not matching.HANDLE.fullmatch(handle) or (normalized and value != handle):
        raise ValueError("native probe requires a normalized exact account handle")
    return handle


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("native probe report contains duplicate JSON fields")
        result[key] = value
    return result


def _read_report(path: Path) -> dict:
    try:
        # Bounded local input; even reports with the maximum diagnostic events
        # are comfortably below this limit. Never echo raw input or file paths.
        with path.open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError
        report = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_json_object)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ValueError("native probe report is unreadable or invalid JSON") from None
    if not isinstance(report, dict):
        raise ValueError("native probe report must be an object")
    return report


def _composition_labels_match(text: str, labels: list[str]) -> bool:
    """Consume native tokens in requested-handle order, even for equal labels.

    Display text is not identity: two handles can share a label, or one label
    can prefix another. Probe composition separates each exact label from its
    prose with ': ', and may not contain any additional mention tokens.
    """
    offset = 0
    for label in labels:
        start = text.find("@", offset)
        token = label + ": "
        if start < 0 or not text.startswith(token, start):
            return False
        offset = start + len(token)
    return text.find("@", offset) < 0


def _report_labels(conn: sqlite3.Connection, run_id: str, account: str, report: dict) -> dict[str, str]:
    if (
        report.get("schema_version") != "engage-mentions-probe-v1"
        or report.get("status") != "passed"
        or report.get("publication_enabled") is not False
        or type(report.get("blocked_publish_requests")) is not int
        or report["blocked_publish_requests"] != 0
        or report.get("editor_cleared") is not True
        or report.get("temporary_tab_closed") is not True
    ):
        raise ValueError("native probe must have passed with publication disabled, no publish requests, and completed cleanup")
    if report.get("expected_account") != account or report.get("observed_account") != account:
        raise ValueError("native probe account differs from the run's exact expected account")
    url = report.get("url")
    if not isinstance(url, str):
        raise ValueError("native probe requires a canonical evidence-ready URL from this run")
    row = conn.execute(
        "SELECT * FROM engage_tiktok_posts WHERE run_id=? AND url=? AND evidence_ready=1",
        (run_id, url),
    ).fetchone()
    if row is None:
        raise ValueError("native probe URL is not an evidence-ready post in this run")
    if not re.fullmatch(r"[0-9]+", str(row["post_id"]), re.ASCII):
        raise ValueError("native probe URL must identify a canonical numeric TikTok post")
    matching._checked(dict(row))
    handles = report.get("handles")
    if not isinstance(handles, list) or not 1 <= len(handles) <= 2:
        raise ValueError("native probe requires one or two distinct creator handles")
    handles = [_handle(value, normalized=True) for value in handles]
    if len(set(handles)) != len(handles):
        raise ValueError("native probe requires distinct creator handles")
    mentions = report.get("observed_mentions")
    if not isinstance(mentions, list) or len(mentions) != len(handles):
        raise ValueError("native probe requires one observed label for each creator")
    labels = {}
    for mention in mentions:
        if not isinstance(mention, dict):
            raise ValueError("native probe observed mention must be an object")
        handle = _handle(mention.get("creator_handle"), normalized=True)
        if handle not in handles or handle in labels:
            raise ValueError("native probe observed creators differ from its requested handles")
        labels[handle] = matching._mention_label(mention.get("mention_label"))
    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != 2:
        raise ValueError("native probe requires exactly two passed composition cases")
    case_ids = set()
    for case in cases:
        if not isinstance(case, dict) or case.get("status") != "passed":
            raise ValueError("native probe composition case did not pass")
        case_id, text = case.get("case"), case.get("text")
        if type(case_id) is not int or case_id not in (1, 2) or case_id in case_ids:
            raise ValueError("native probe composition cases must be numbered one and two")
        case_ids.add(case_id)
        if (
            not isinstance(text, str) or not text.strip()
            or any(unicodedata.category(character)[0] == "C"
                   or unicodedata.category(character) in {"Zl", "Zp"}
                   for character in text)
            or not _composition_labels_match(text, [labels[handle] for handle in handles])
        ):
            raise ValueError("native probe composition must be one paragraph with exactly the requested labels in order")
    return labels


def bind_native_labels(
    conn: sqlite3.Connection, run_id: str, records: list[dict], report_paths: list[Path],
) -> list[dict]:
    """Return copied choices with exact observed labels; perform SELECTs only.

    Labels may be reused for a candidate within this run. Every report must
    rehearse on a canonical evidence-ready member of this run and use its
    expected operator account. Existing match/hash and approval gates remain
    the responsibility of the ordinary import and publication paths.
    """
    run = conn.execute("SELECT expected_account FROM engage_tiktok_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError("native probe binding requires an existing run")
    account = _handle(run["expected_account"])
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("native probe binding requires a list of creator matching records")
    if not isinstance(report_paths, list) or not report_paths:
        raise ValueError("native probe binding requires at least one passed report")
    labels = {}
    for path in report_paths:
        try:
            local_path = Path(path)
        except (TypeError, ValueError):
            raise ValueError("native probe report path is invalid") from None
        report_labels = _report_labels(conn, run_id, account, _read_report(local_path))
        for handle, label in report_labels.items():
            if handle in labels and labels[handle] != label:
                raise ValueError("native probe reports contain conflicting labels for one creator")
            labels[handle] = label
    result = deepcopy(records)
    for record in result:
        if "run_id" in record and record["run_id"] != run_id:
            raise ValueError("native probe matching record belongs to another run")
        source = matching._row(conn, run_id, str(record.get("post_id") or ""))
        _, _, source_handle = matching._checked(source)
        matches = record.get("matches")
        if not isinstance(matches, list) or len(matches) > 2:
            raise ValueError("native probe matching record requires at most two creator choices")
        seen = {source_handle}
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError("native probe creator choice must be an object")
            target = matching._row(conn, run_id, str(match.get("post_id") or ""))
            _, _, handle = matching._checked(target)
            if handle in seen:
                raise ValueError("native probe choices cannot mention self or duplicate creators")
            seen.add(handle)
            if "creator_handle" in match and _handle(match["creator_handle"]) != handle:
                raise ValueError("native probe candidate handle differs from its exact stored owner")
            if handle not in labels:
                raise ValueError("native probe report is missing a selected creator's observed label")
            if "mention_label" in match and match["mention_label"] != labels[handle]:
                raise ValueError("supplied creator label conflicts with the observed native label")
            match["mention_label"] = labels[handle]
    return result
