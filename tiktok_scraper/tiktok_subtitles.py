"""TikTok web-player subtitle extraction and timed-text parsing."""

from __future__ import annotations

import asyncio
import html
from html.parser import HTMLParser
import json
import re
from typing import Any, Iterable


DEFAULT_PREFERRED_LANGUAGES = ("id", "en")
DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_MAX_BYTES = 2_000_000

LANGUAGE_ALIASES = {
    "alb": "sq",
    "ara": "ar",
    "aze": "az",
    "ben": "bn",
    "bul": "bg",
    "cat": "ca",
    "ceb": "ceb",
    "ces": "cs",
    "cmn": "zh",
    "dan": "da",
    "deu": "de",
    "ell": "el",
    "eng": "en",
    "est": "et",
    "fil": "fil",
    "fin": "fi",
    "fra": "fr",
    "gle": "ga",
    "heb": "he",
    "hin": "hi",
    "hrv": "hr",
    "hun": "hu",
    "ind": "id",
    "isl": "is",
    "ita": "it",
    "jav": "jv",
    "jpn": "ja",
    "kaz": "kk",
    "khm": "km",
    "kor": "ko",
    "lav": "lv",
    "lit": "lt",
    "msa": "ms",
    "mya": "my",
    "nld": "nl",
    "nob": "nb",
    "pol": "pl",
    "por": "pt",
    "ron": "ro",
    "rus": "ru",
    "slk": "sk",
    "slv": "sl",
    "spa": "es",
    "swa": "sw",
    "swe": "sv",
    "tha": "th",
    "tur": "tr",
    "ukr": "uk",
    "urd": "ur",
    "uzb": "uz",
    "vie": "vi",
}

LANGUAGE_NAMES = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fil": "Filipino",
    "fr": "French",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "jv": "Javanese",
    "ko": "Korean",
    "ms": "Malay",
    "nl": "Dutch",
    "pt": "Portuguese",
    "ru": "Russian",
    "th": "Thai",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "zh": "Chinese",
}


class TikTokSubtitleError(RuntimeError):
    """Raised for a bounded subtitle metadata or timed-text failure."""


class _ScriptByIdParser(HTMLParser):
    def __init__(self, target_ids: Iterable[str]):
        super().__init__(convert_charrefs=False)
        self.target_ids = set(target_ids)
        self.current_id = ""
        self.buffers: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        attributes = {str(key).casefold(): value for key, value in attrs}
        script_id = str(attributes.get("id") or "")
        if script_id in self.target_ids:
            self.current_id = script_id
            self.buffers.setdefault(script_id, [])

    def handle_data(self, data: str) -> None:
        if self.current_id:
            self.buffers[self.current_id].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script":
            self.current_id = ""


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _language_parts(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return "", ""
    parts = [part for part in raw.split("-") if part]
    if not parts:
        return "", ""
    base = LANGUAGE_ALIASES.get(parts[0].casefold(), parts[0].casefold())
    region = ""
    if len(parts) > 1:
        region = parts[-1].upper() if len(parts[-1]) in {2, 3} else parts[-1]
    return base, f"{base}-{region}" if region else base


def parse_preferred_languages(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        raw_values = list(DEFAULT_PREFERRED_LANGUAGES)
    elif isinstance(value, str):
        raw_values = re.split(r"[,;|\s]+", value)
    else:
        raw_values = [str(item) for item in value]
    preferred: list[str] = []
    for raw in raw_values:
        base, tag = _language_parts(raw)
        language = tag or base
        if language and language not in preferred:
            preferred.append(language)
    return preferred or list(DEFAULT_PREFERRED_LANGUAGES)


def _url_candidates(row: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        row.get("Url"),
        row.get("url"),
        row.get("DownloadAddr"),
        row.get("download_url"),
    ]
    for name in ("UrlList", "urlList", "url_list"):
        nested = row.get(name)
        if isinstance(nested, list):
            values.extend(nested)
    urls: list[str] = []
    for value in values:
        url = str(value or "").strip()
        if url.startswith(("https://", "http://")) and url not in urls:
            urls.append(url)
    return urls


def _normalized_track(row: dict[str, Any], *, source_container: str) -> dict[str, Any] | None:
    raw_language = (
        row.get("LanguageCodeName")
        or row.get("language")
        or row.get("languageCodeName")
        or row.get("language_code")
    )
    language, language_tag = _language_parts(raw_language)
    urls = _url_candidates(row)
    if not language and not urls:
        return None

    source = str(row.get("Source") or row.get("source") or "").strip().upper()
    version = str(
        row.get("Version")
        or row.get("version")
        or row.get("variant")
        or row.get("subtitleType")
        or ""
    ).strip()
    is_original_value = row.get("isOriginalCaption")
    translated = source == "MT" or str(row.get("translationType") or "0") not in {"", "0"}
    is_original = _bool(is_original_value) if is_original_value is not None else source == "ASR"
    auto_value = row.get("isAutoGen")
    is_auto_generated = (
        _bool(auto_value)
        if auto_value is not None
        else source in {"ASR", "MT"} or "whisper" in version.casefold()
    )
    return {
        "language": language,
        "language_tag": language_tag or language,
        "language_raw": str(raw_language or ""),
        "language_name": LANGUAGE_NAMES.get(language, language_tag or language),
        "format": str(row.get("Format") or row.get("captionFormat") or "webvtt").casefold(),
        "version": version,
        "source": source,
        "is_original": is_original,
        "is_auto_generated": is_auto_generated,
        "is_translated": translated,
        "expires_at": str(row.get("UrlExpire") or row.get("expire") or ""),
        "size_bytes": _safe_int(row.get("Size") or row.get("size")),
        "subtitle_id": str(row.get("claSubtitleID") or row.get("subID") or row.get("id") or ""),
        "source_container": source_container,
        "url": urls[0] if urls else "",
        "urls": urls,
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_tracks(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "urls":
            merged[key] = list(dict.fromkeys([*(existing.get(key) or []), *(value or [])]))
        elif key in {"is_original", "is_auto_generated", "is_translated"}:
            merged[key] = bool(existing.get(key) or value)
        elif value not in (None, "", [], {}):
            if key == "source" and existing.get("source") and not incoming.get("is_original"):
                continue
            merged[key] = value
    if not merged.get("url") and merged.get("urls"):
        merged["url"] = merged["urls"][0]
    return merged


def extract_tiktok_subtitle_manifest(item: dict[str, Any]) -> dict[str, Any]:
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    cla = video.get("claInfo") or video.get("cla_info") or {}
    if not isinstance(cla, dict):
        cla = {}
    observed = any(
        key in video
        for key in ("subtitleInfos", "subtitle_infos", "claInfo", "cla_info")
    )
    subtitle_rows = video.get("subtitleInfos") or video.get("subtitle_infos") or []
    caption_rows = cla.get("captionInfos") or cla.get("caption_infos") or []

    tracks_by_language: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rows, container in (
        (subtitle_rows, "video.subtitleInfos"),
        (caption_rows, "video.claInfo.captionInfos"),
    ):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            track = _normalized_track(row, source_container=container)
            if not track:
                continue
            key = track.get("language_tag") or track.get("language") or track.get("url")
            if key not in tracks_by_language:
                tracks_by_language[key] = track
                order.append(key)
            else:
                tracks_by_language[key] = _merge_tracks(tracks_by_language[key], track)

    return {
        "observed": observed,
        "item_id": str(item.get("id") or item.get("aweme_id") or item.get("item_id") or ""),
        "tracks": [tracks_by_language[key] for key in order],
        "auto_caption_enabled": cla.get("enableAutoCaption"),
        "has_original_audio": cla.get("hasOriginalAudio"),
        "no_caption_reason": cla.get("noCaptionReason"),
        "manifest_source": "tiktok_item_payload" if observed else "",
    }


def _walk_for_item(value: Any, expected_video_id: str = "") -> dict[str, Any] | None:
    if isinstance(value, dict):
        item_id = str(value.get("id") or value.get("aweme_id") or value.get("item_id") or "")
        video = value.get("video")
        if isinstance(video, dict) and (
            not expected_video_id or not item_id or item_id == str(expected_video_id)
        ):
            if any(key in video for key in ("subtitleInfos", "subtitle_infos", "claInfo", "cla_info")):
                return value
        for nested in value.values():
            found = _walk_for_item(nested, expected_video_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _walk_for_item(nested, expected_video_id)
            if found is not None:
                return found
    return None


def extract_tiktok_item_from_html(html_text: str, expected_video_id: str = "") -> dict[str, Any] | None:
    parser = _ScriptByIdParser(("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"))
    parser.feed(html_text or "")
    payloads: list[dict[str, Any]] = []
    for parts in parser.buffers.values():
        raw = "".join(parts).strip()
        if not raw:
            continue
        for candidate in (raw, html.unescape(raw)):
            try:
                payload = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
                break

    for payload in payloads:
        scope = payload.get("__DEFAULT_SCOPE__")
        if isinstance(scope, dict):
            detail = scope.get("webapp.video-detail") or scope.get("webapp.video_detail")
            if isinstance(detail, dict):
                item_info = detail.get("itemInfo") or detail.get("item_info") or {}
                if isinstance(item_info, dict):
                    item = item_info.get("itemStruct") or item_info.get("item_struct")
                    if isinstance(item, dict):
                        return item
        item_module = payload.get("ItemModule")
        if isinstance(item_module, dict):
            if expected_video_id and isinstance(item_module.get(str(expected_video_id)), dict):
                return item_module[str(expected_video_id)]
            for item in item_module.values():
                if isinstance(item, dict):
                    return item
        found = _walk_for_item(payload, expected_video_id)
        if found is not None:
            return found
    return None


def extract_tiktok_subtitle_manifest_from_html(
    html_text: str,
    expected_video_id: str = "",
) -> dict[str, Any]:
    item = extract_tiktok_item_from_html(html_text, expected_video_id)
    if item is None:
        return {
            "observed": False,
            "item_id": str(expected_video_id or ""),
            "tracks": [],
            "auto_caption_enabled": None,
            "has_original_audio": None,
            "no_caption_reason": None,
            "manifest_source": "",
        }
    manifest = extract_tiktok_subtitle_manifest(item)
    manifest["manifest_source"] = "tiktok_web_rehydration"
    return manifest


def _matches_preference(track: dict[str, Any], preference: str) -> bool:
    preferred_base, preferred_tag = _language_parts(preference)
    track_base, track_tag = _language_parts(track.get("language_tag") or track.get("language"))
    return bool(
        preferred_base
        and track_base == preferred_base
        and ("-" not in preferred_tag or track_tag == preferred_tag)
    )


def select_tiktok_subtitle_track(
    tracks: Iterable[dict[str, Any]],
    preferred_languages: str | Iterable[str] | None = None,
) -> dict[str, Any] | None:
    preferred = parse_preferred_languages(preferred_languages)
    eligible = [dict(track) for track in tracks if str(track.get("url") or "").startswith("http")]
    if not eligible:
        return None

    def rank(track: dict[str, Any]) -> tuple[int, int, int, int, str]:
        matches = [index for index, value in enumerate(preferred) if _matches_preference(track, value)]
        preference_index = matches[0] if matches else len(preferred) + 1
        return (
            0 if matches else 1,
            preference_index,
            0 if track.get("is_original") else 1,
            1 if track.get("is_translated") else 0,
            str(track.get("language_tag") or track.get("language") or ""),
        )

    return min(eligible, key=rank)


def _timestamp_seconds(value: str) -> float:
    parts = value.strip().replace(",", ".").split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def _clean_cue_text(value: str) -> str:
    text = re.sub(r"<v(?:\.[^ >]+)*\s+([^>]+)>", r"\1: ", value, flags=re.I)
    text = re.sub(r"<\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\ufeff", "").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_tiktok_timed_text(value: str) -> list[dict[str, Any]]:
    lines = (value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip().lstrip("\ufeff")
        if not line or line.upper().startswith("WEBVTT"):
            index += 1
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        if "-->" not in line and index + 1 < len(lines) and "-->" in lines[index + 1]:
            index += 1
            line = lines[index].strip()
        cue_match = re.match(r"^([^\s]+)\s+-->\s+([^\s]+)", line)
        if not cue_match:
            index += 1
            continue
        start_raw, end_raw = cue_match.groups()
        index += 1
        cue_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            cue_lines.append(lines[index])
            index += 1
        cue_text = _clean_cue_text(" ".join(cue_lines))
        if cue_text and (not segments or segments[-1]["text"] != cue_text):
            segments.append(
                {
                    "start": start_raw.replace(",", "."),
                    "end": end_raw.replace(",", "."),
                    "start_seconds": round(_timestamp_seconds(start_raw), 3),
                    "end_seconds": round(_timestamp_seconds(end_raw), 3),
                    "text": cue_text,
                }
            )
        index += 1
    return segments


def public_track_metadata(track: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in track.items()
        if key not in {"url", "urls"} and value not in (None, "", [], {})
    }


def empty_tiktok_transcript_result(status: str = "unavailable") -> dict[str, Any]:
    return {
        "transcript": "",
        "transcript_available": False,
        "transcript_language": "",
        "transcript_language_name": "",
        "transcript_is_auto_generated": False,
        "transcript_segment_count": 0,
        "transcript_segments": [],
        "transcript_error": "",
        "transcript_status": status,
        "transcript_source": "",
        "transcript_method": "tiktok_web_subtitle_api" if status != "disabled" else "disabled",
        "transcript_attempt_count": 0,
        "subtitle_tracks": [],
        "subtitle_selected_track": {},
        "subtitle_no_caption_reason": "",
        "subtitle_manifest_source": "",
    }


def _request_context(page: Any) -> Any:
    request = getattr(page, "request", None)
    if request is not None:
        return request
    context = getattr(page, "context", None)
    request = getattr(context, "request", None)
    if request is None:
        raise TikTokSubtitleError("browser request context is unavailable")
    return request


async def _response_text(response: Any) -> str:
    method = getattr(response, "text", None)
    if method is None:
        raise TikTokSubtitleError("response body is unavailable")
    value = method()
    if hasattr(value, "__await__"):
        value = await value
    return str(value or "")


async def _get_text(
    page: Any,
    url: str,
    *,
    timeout_ms: int,
    retries: int,
    headers: dict[str, str],
) -> tuple[str, int]:
    request = _request_context(page)
    attempts = 0
    last_error = "request failed"
    for attempt in range(max(0, retries) + 1):
        attempts += 1
        try:
            response = await request.get(url, headers=headers, timeout=timeout_ms)
            ok = bool(getattr(response, "ok", False))
            status = _safe_int(getattr(response, "status", None))
            if not ok:
                raise TikTokSubtitleError(f"HTTP {status or 'error'}")
            return await _response_text(response), attempts
        except Exception as exc:
            last_error = str(exc) or type(exc).__name__
            if attempt < max(0, retries):
                await asyncio.sleep(0.5 * (attempt + 1))
    raise TikTokSubtitleError(last_error)


async def collect_tiktok_transcript(
    page: Any,
    video: dict[str, Any],
    *,
    preferred_languages: str | Iterable[str] | None = None,
    enabled: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    retries: int = 1,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    result = empty_tiktok_transcript_result("unavailable" if enabled else "disabled")
    if not enabled:
        return result

    video_id = str(video.get("id") or video.get("video_id") or "")
    video_url = str(video.get("url") or video.get("video_url") or "").strip()
    manifest = video.get("_tiktok_subtitle_manifest")
    if not isinstance(manifest, dict):
        manifest = {}

    manifest_tracks = [
        track for track in manifest.get("tracks") or [] if isinstance(track, dict)
    ]
    has_downloadable_track = any(str(track.get("url") or "").startswith("http") for track in manifest_tracks)
    should_hydrate_manifest = not manifest.get("observed") or (
        not has_downloadable_track
        and manifest.get("no_caption_reason") in {None, ""}
        and manifest.get("manifest_source") != "tiktok_web_rehydration"
    )
    if should_hydrate_manifest:
        if not video_url:
            result["transcript_status"] = "metadata_error"
            result["transcript_error"] = "missing_video_url"
            return result
        try:
            metadata_html, attempts = await _get_text(
                page,
                video_url,
                timeout_ms=max(1_000, int(timeout_ms)),
                retries=max(0, int(retries)),
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://www.tiktok.com/",
                },
            )
            result["transcript_attempt_count"] += attempts
            manifest = extract_tiktok_subtitle_manifest_from_html(metadata_html, video_id)
        except TikTokSubtitleError as exc:
            result["transcript_status"] = "metadata_error"
            result["transcript_error"] = str(exc)
            return result

    result["subtitle_manifest_source"] = str(manifest.get("manifest_source") or "")
    result["subtitle_no_caption_reason"] = (
        "" if manifest.get("no_caption_reason") is None else str(manifest.get("no_caption_reason"))
    )
    tracks = [track for track in manifest.get("tracks") or [] if isinstance(track, dict)]
    result["subtitle_tracks"] = [public_track_metadata(track) for track in tracks]
    selected = select_tiktok_subtitle_track(tracks, preferred_languages)
    if selected is None:
        return result

    result["subtitle_selected_track"] = public_track_metadata(selected)
    track_url = str(selected.get("url") or "")
    try:
        timed_text, attempts = await _get_text(
            page,
            track_url,
            timeout_ms=max(1_000, int(timeout_ms)),
            retries=max(0, int(retries)),
            headers={
                "Accept": "text/vtt,text/plain,*/*",
                "Referer": video_url or "https://www.tiktok.com/",
            },
        )
        result["transcript_attempt_count"] += attempts
    except TikTokSubtitleError as exc:
        result["transcript_status"] = "fetch_error"
        result["transcript_error"] = str(exc)
        return result

    if len(timed_text.encode("utf-8", errors="replace")) > max(1_024, int(max_bytes)):
        result["transcript_status"] = "response_too_large"
        result["transcript_error"] = "subtitle_response_exceeded_limit"
        return result

    segments = parse_tiktok_timed_text(timed_text)
    if not segments:
        result["transcript_status"] = "empty_track"
        result["transcript_error"] = "subtitle_track_contained_no_timed_cues"
        return result

    transcript = "\n".join(segment["text"] for segment in segments).strip()
    result.update(
        {
            "transcript": transcript,
            "transcript_available": bool(transcript),
            "transcript_language": str(selected.get("language") or ""),
            "transcript_language_name": str(selected.get("language_name") or ""),
            "transcript_is_auto_generated": bool(selected.get("is_auto_generated")),
            "transcript_segment_count": len(segments),
            "transcript_segments": segments,
            "transcript_error": "",
            "transcript_status": "ok",
            "transcript_source": "tiktok_web_subtitle_api",
            "transcript_method": "tiktok_web_subtitle_api",
        }
    )
    return result
