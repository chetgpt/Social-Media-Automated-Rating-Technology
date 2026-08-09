from typing import List, Dict, Any, Optional
from .base_scraper import BaseScraper
import asyncio
from playwright.async_api import async_playwright, Page, BrowserContext
import json
import re
import os
import threading
import time
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse

class YouTubeScraper(BaseScraper):
    def __init__(self):
        super().__init__("youtube")
        self._innertube_config_cache: Optional[Dict[str, Any]] = None
        self._transcript_state_lock = threading.RLock()
        self._transcript_request_lock = threading.Lock()
        self._transcript_blocked_until = 0.0
        self._transcript_circuit_reason = ""
        self._transcript_failure_streak = 0
        self._transcript_last_started_at = 0.0

    def extract_video_id(self, url: str) -> str:
        """Extract a YouTube video id from watch, short, embed, or youtu.be URLs."""
        if not url:
            return ""

        parsed = urlparse(url)
        if parsed.hostname and "youtu.be" in parsed.hostname:
            return parsed.path.strip("/").split("/")[0]

        query_id = parse_qs(parsed.query).get("v", [""])[0]
        if query_id:
            return query_id

        match = re.search(r"/(?:shorts|embed|live)/([^/?#]+)", parsed.path)
        if match:
            return match.group(1)

        return ""

    def is_channel_url(self, value: str) -> bool:
        parsed = urlparse(value or "")
        if not parsed.hostname or "youtube.com" not in parsed.hostname.lower():
            return False
        parts = [part for part in parsed.path.split("/") if part]
        return bool(parts and (parts[0].startswith("@") or parts[0] in {"channel", "c", "user"}))

    def _channel_inventory_from_url_sync(self, channel_url: str, max_videos: int = 0) -> List[Dict[str, Any]]:
        from youtube_comment_downloader import YoutubeCommentDownloader

        parsed = urlparse(channel_url)
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[-1].lower() in {"videos", "streams", "shorts", "featured"}:
            parts = parts[:-1]
        base_url = f"https://www.youtube.com/{'/'.join(parts)}".rstrip("/")
        title = parts[-1].lstrip("@") if parts else base_url
        downloader = YoutubeCommentDownloader()
        return self._channel_inventory_with_innertube(
            downloader,
            {"url": base_url, "title": title},
            max_videos=max_videos,
        )

    def _text_from_runs(self, obj: Any) -> str:
        if not isinstance(obj, dict):
            return ""
        if "simpleText" in obj:
            return obj.get("simpleText") or ""
        return "".join(
            run.get("text", "")
            for run in obj.get("runs", [])
            if isinstance(run, dict)
        )

    def _iter_dict_values(self, node: Any, key: str):
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for current_key, value in current.items():
                    if current_key == key:
                        yield value
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)

    def _compact_public_count(self, value: Any) -> Optional[int]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return max(0, int(value))
        text = str(value).replace("\u00a0", " ").strip().lower()
        match = re.search(r"(\d[\d.,]*)\s*(k|m|b|rb|ribu|jt|juta)?", text)
        if not match:
            return None
        number = match.group(1)
        suffix = match.group(2) or ""
        if suffix and "," in number and "." not in number:
            number = number.replace(",", ".")
        elif not suffix:
            number = re.sub(r"[.,](?=\d{3}(?:\D|$))", "", number)
        number = number.replace(",", "")
        try:
            numeric = float(number)
        except ValueError:
            return None
        multiplier = {
            "": 1,
            "k": 1_000,
            "rb": 1_000,
            "ribu": 1_000,
            "m": 1_000_000,
            "jt": 1_000_000,
            "juta": 1_000_000,
            "b": 1_000_000_000,
        }[suffix]
        return max(0, int(numeric * multiplier))

    def _short_text_values(self, node: Any):
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, str):
                text = current.strip()
                if text and len(text) <= 160:
                    yield text
            elif isinstance(current, dict):
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    def _structured_count(self, value: Any) -> Optional[int]:
        """Parse counts only from display-text fields, never URLs or tracking tokens."""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (str, int, float)):
            return self._compact_public_count(value)
        if not isinstance(value, dict):
            return None

        candidates = []
        for key in ("simpleText", "content", "title", "label", "accessibilityText"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
                candidates.append(candidate)
        runs_text = self._text_from_runs(value)
        if runs_text:
            candidates.append(runs_text)
        for candidate in candidates:
            count = self._compact_public_count(candidate)
            if count is not None:
                return count
        return None

    def _count_from_named_fields(
        self,
        node: Any,
        field_names: tuple[str, ...],
        labels: tuple[str, ...] = (),
    ) -> Optional[int]:
        for field_name in field_names:
            for value in self._iter_dict_values(node, field_name):
                if labels:
                    texts = list(self._short_text_values(value))
                    if not any(
                        any(label in text.casefold() for label in labels)
                        for text in texts
                    ):
                        continue
                count = self._structured_count(value)
                if count is not None:
                    return count
        return None

    def _count_from_renderer_branches(
        self,
        payload: Dict[str, Any],
        renderer_keys: tuple[str, ...],
        labels: tuple[str, ...],
    ) -> Optional[int]:
        for renderer_key in renderer_keys:
            for branch in self._iter_dict_values(payload, renderer_key):
                if not isinstance(branch, dict):
                    continue

                scoped_branches = [branch]
                if renderer_key.startswith("segmentedLikeDislike"):
                    like_branches = []
                    for like_key in ("likeButtonViewModel", "likeButtonRenderer"):
                        like_branches.extend(self._iter_dict_values(branch, like_key))
                    if like_branches:
                        scoped_branches = [value for value in like_branches if isinstance(value, dict)]

                for scoped_branch in scoped_branches:
                    # The default button is the current count. The toggled button can
                    # contain the post-like count, which is one higher.
                    for default_branch in self._iter_dict_values(
                        scoped_branch, "defaultButtonViewModel"
                    ):
                        count = self._count_from_named_fields(
                            default_branch,
                            ("title", "defaultText", "text"),
                        )
                        if count is not None:
                            return count

                    count = self._count_from_named_fields(scoped_branch, ("defaultText",))
                    if count is not None:
                        return count
                    count = self._count_from_named_fields(
                        scoped_branch,
                        ("accessibilityText", "accessibilityLabel", "label"),
                        labels,
                    )
                    if count is not None:
                        return count
                    count = self._count_from_named_fields(scoped_branch, ("title",))
                    if count is not None:
                        return count
        return None

    def _extract_next_engagement(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        like_count = None
        for key in ("likeCount", "like_count"):
            for value in self._iter_dict_values(payload, key):
                like_count = self._compact_public_count(value)
                if like_count is not None:
                    break
            if like_count is not None:
                break
        if like_count is None:
            like_count = self._count_from_renderer_branches(
                payload,
                (
                    "segmentedLikeDislikeButtonViewModel",
                    "segmentedLikeDislikeButtonRenderer",
                    "likeButtonViewModel",
                    "toggleButtonRenderer",
                ),
                ("like", "likes", "suka"),
            )

        subscriber_count = None
        for value in self._iter_dict_values(payload, "subscriberCountText"):
            subscriber_count = self._structured_count(value)
            if subscriber_count is not None:
                break
        return {
            "like_count": like_count,
            "follower_count": subscriber_count,
        }

    def _normalized_label(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    def _near_label(self, left: str, right: str, max_distance: int = 1) -> bool:
        if left == right:
            return True
        if abs(len(left) - len(right)) > max_distance:
            return False
        previous = list(range(len(right) + 1))
        for i, left_char in enumerate(left, start=1):
            current = [i]
            row_min = current[0]
            for j, right_char in enumerate(right, start=1):
                cost = 0 if left_char == right_char else 1
                current.append(min(
                    current[j - 1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + cost,
                ))
                row_min = min(row_min, current[-1])
            if row_min > max_distance:
                return False
            previous = current
        return previous[-1] <= max_distance

    def _transcript_defaults(
        self,
        error: str = "",
        *,
        status: str = "",
        source: str = "",
        attempts: int = 0,
    ) -> Dict[str, Any]:
        if not status:
            if error == "disabled":
                status = "disabled"
            elif error:
                status = "error"
            else:
                status = "not_requested"
        return {
            "transcript": "",
            "transcript_available": False,
            "transcript_language": "",
            "transcript_language_name": "",
            "transcript_is_auto_generated": False,
            "transcript_segment_count": 0,
            "transcript_error": error,
            "transcript_status": status,
            "transcript_source": source,
            "transcript_attempt_count": attempts,
        }

    def _transcripts_enabled(self) -> bool:
        value = os.environ.get("YOUTUBE_SCRAPE_TRANSCRIPTS", "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _preferred_transcript_languages(self) -> List[str]:
        raw = os.environ.get("YOUTUBE_TRANSCRIPT_LANGS", "id,en")
        languages = [part.strip().lower() for part in raw.split(",") if part.strip()]
        return languages or ["id", "en"]

    def _env_float(self, name: str, default: float) -> float:
        try:
            return max(0.0, float(os.environ.get(name, str(default))))
        except ValueError:
            return default

    def _env_int(self, name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, str(default))))
        except ValueError:
            return default

    def _compact_transcript_error(self, exc: BaseException) -> str:
        message = re.sub(r"\s+", " ", str(exc)).strip()
        if len(message) > 400:
            message = f"{message[:397]}..."
        return f"{type(exc).__name__}: {message}" if message else type(exc).__name__

    def _transcript_circuit_result(self) -> Optional[Dict[str, Any]]:
        with self._transcript_state_lock:
            remaining = self._transcript_blocked_until - time.monotonic()
            if remaining <= 0:
                self._transcript_blocked_until = 0.0
                self._transcript_circuit_reason = ""
                return None
            reason = self._transcript_circuit_reason or "request_blocked"
        return self._transcript_defaults(
            f"transcript_circuit_open: {reason}; retry_after={int(remaining + 0.999)}s",
            status="circuit_open",
            source="youtube_transcript_api",
        )

    def _open_transcript_circuit(self, reason: str) -> None:
        cooldown = self._env_float("YOUTUBE_TRANSCRIPT_COOLDOWN_SECONDS", 900.0)
        if cooldown <= 0:
            return
        with self._transcript_state_lock:
            self._transcript_blocked_until = max(
                self._transcript_blocked_until,
                time.monotonic() + cooldown,
            )
            self._transcript_circuit_reason = reason

    def _register_transcript_result(self, status: str, error: str = "") -> None:
        if status in {"ok", "unavailable", "disabled"}:
            with self._transcript_state_lock:
                self._transcript_failure_streak = 0
            return

        if status in {"rate_limited", "blocked"}:
            self._open_transcript_circuit(error or status)
            return

        guarded_statuses = {"po_token_required", "request_failed", "protocol_error"}
        if status not in guarded_statuses:
            return
        threshold = max(1, self._env_int("YOUTUBE_TRANSCRIPT_CIRCUIT_FAILURES", 3))
        with self._transcript_state_lock:
            self._transcript_failure_streak += 1
            should_open = self._transcript_failure_streak >= threshold
        if should_open:
            self._open_transcript_circuit(error or status)

    def _wait_for_transcript_slot(self) -> None:
        delay = self._env_float("YOUTUBE_TRANSCRIPT_DELAY_SECONDS", 0.75)
        with self._transcript_request_lock:
            elapsed = time.monotonic() - self._transcript_last_started_at
            if delay > elapsed:
                time.sleep(delay - elapsed)
            self._transcript_last_started_at = time.monotonic()

    def _transcript_player_method(self) -> str:
        value = (
            os.environ.get("YOUTUBE_TRANSCRIPT_PLAYER_METHOD")
            or os.environ.get("YOUTUBE_TRANSCRIPT_METHOD")
            or "api"
        ).strip().lower()
        aliases = {
            "api": "api",
            "innertube": "api",
            "youtubei": "api",
            "player-api": "api",
            "watch": "watch",
            "html": "watch",
            "page": "watch",
            "auto": "auto",
            "fallback": "auto",
        }
        if value not in aliases:
            self.logger.warning("Ignoring invalid YOUTUBE_TRANSCRIPT_PLAYER_METHOD=%r; using api", value)
            return "api"
        return aliases[value]

    def _youtube_user_agent(self) -> str:
        return os.environ.get(
            "YOUTUBE_USER_AGENT",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

    def _youtube_headers(self, *, referer: str = "", accept: str = "*/*") -> Dict[str, str]:
        headers = {
            "User-Agent": self._youtube_user_agent(),
            "Accept-Language": os.environ.get(
                "YOUTUBE_ACCEPT_LANGUAGE",
                "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            ),
            "Accept": accept,
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _extract_balanced_json(self, text: str, marker: str) -> Dict[str, Any]:
        marker_index = text.find(marker)
        while marker_index >= 0:
            start = text.find("{", marker_index)
            if start < 0:
                return {}

            depth = 0
            in_string = False
            quote_char = ""
            escaped = False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote_char:
                        in_string = False
                    continue

                if char in {'"', "'"}:
                    in_string = True
                    quote_char = char
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:index + 1])
                        except json.JSONDecodeError:
                            break

            marker_index = text.find(marker, marker_index + len(marker))
        return {}

    def _watch_url_for_video(self, video_data: Dict[str, Any]) -> str:
        video_url = video_data.get("url") or ""
        if video_url:
            return video_url
        video_id = video_data.get("video_id") or ""
        return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

    def _fetch_player_response_sync(self, video_url: str, session: Any) -> Dict[str, Any]:
        if not video_url:
            return {}

        method = self._transcript_player_method()
        errors = []
        if method in {"api", "auto"}:
            try:
                return self._fetch_player_response_with_innertube_sync(video_url, session)
            except Exception as exc:
                errors.append(f"api={exc}")
                if method == "api":
                    raise

        if method in {"watch", "auto"}:
            try:
                headers = self._youtube_headers(
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                )
                response = session.get(video_url, headers=headers, timeout=30)
                response.raise_for_status()
                player_response = self._extract_balanced_json(response.text, "ytInitialPlayerResponse")
                if player_response:
                    return player_response
                errors.append("watch=no_player_response")
            except Exception as exc:
                errors.append(f"watch={exc}")

        raise RuntimeError("; ".join(errors) or "player_response_unavailable")

    def _fetch_innertube_config_sync(self, session: Any) -> Dict[str, Any]:
        if self._innertube_config_cache is not None:
            return self._innertube_config_cache

        config_url = os.environ.get("YOUTUBE_INNERTUBE_CONFIG_URL", "https://www.youtube.com/")
        response = session.get(
            config_url,
            headers=self._youtube_headers(
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            timeout=30,
        )
        response.raise_for_status()
        config = self._extract_balanced_json(response.text, "ytcfg.set")
        if not config:
            config = self._extract_balanced_json(response.text, "ytcfg")
        self._innertube_config_cache = config or {}
        return self._innertube_config_cache

    def _innertube_context(self, config: Dict[str, Any]) -> Dict[str, Any]:
        context = config.get("INNERTUBE_CONTEXT")
        if isinstance(context, dict) and context.get("client"):
            return context

        client_version = (
            os.environ.get("YOUTUBE_INNERTUBE_CLIENT_VERSION")
            or config.get("INNERTUBE_CLIENT_VERSION")
            or "2.20260706.00.00"
        )
        client = {
            "hl": os.environ.get("YOUTUBE_HL") or config.get("HL") or "en",
            "gl": os.environ.get("YOUTUBE_GL") or config.get("GL") or "ID",
            "clientName": os.environ.get("YOUTUBE_INNERTUBE_CLIENT_NAME") or config.get("INNERTUBE_CLIENT_NAME") or "WEB",
            "clientVersion": client_version,
        }
        visitor_data = os.environ.get("YOUTUBE_VISITOR_DATA") or config.get("VISITOR_DATA")
        if visitor_data:
            client["visitorData"] = visitor_data
        return {"client": client}

    def _fetch_player_response_with_innertube_sync(self, video_url: str, session: Any) -> Dict[str, Any]:
        video_id = self.extract_video_id(video_url)
        if not video_id:
            raise ValueError("missing_video_id")

        config = self._fetch_innertube_config_sync(session)
        context = self._innertube_context(config)
        payload = {
            "context": context,
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }
        return self._innertube_post_sync(
            session,
            config,
            "player",
            payload,
            referer=f"https://www.youtube.com/watch?v={video_id}",
            endpoint_override=os.environ.get("YOUTUBE_INNERTUBE_PLAYER_URL") or config.get("INNERTUBE_API_ENDPOINT") or "",
        )

    def _innertube_url(self, config: Dict[str, Any], endpoint_name: str, endpoint_override: str = "") -> str:
        api_key = os.environ.get("YOUTUBE_INNERTUBE_API_KEY") or config.get("INNERTUBE_API_KEY") or ""
        endpoint = endpoint_override or f"/youtubei/v1/{endpoint_name}"
        if endpoint.startswith("/"):
            endpoint = f"https://www.youtube.com{endpoint}"
        params = {"prettyPrint": "false"}
        if api_key:
            params["key"] = api_key
        return f"{endpoint}?{urlencode(params)}"

    def _innertube_post_sync(
        self,
        session: Any,
        config: Dict[str, Any],
        endpoint_name: str,
        payload: Dict[str, Any],
        *,
        referer: str,
        endpoint_override: str = "",
    ) -> Dict[str, Any]:
        api_url = self._innertube_url(config, endpoint_name, endpoint_override)
        context = payload.get("context") if isinstance(payload, dict) else {}
        client = context.get("client", {}) if isinstance(context, dict) else {}
        headers = self._youtube_headers(
            referer=referer,
            accept="application/json,*/*",
        )
        headers.update({
            "Content-Type": "application/json",
            "Origin": "https://www.youtube.com",
        })
        if client.get("clientVersion"):
            headers["X-YouTube-Client-Version"] = str(client["clientVersion"])
        header_client_name = os.environ.get("YOUTUBE_INNERTUBE_HEADER_CLIENT_NAME") or config.get("INNERTUBE_CONTEXT_CLIENT_NAME")
        if header_client_name:
            headers["X-YouTube-Client-Name"] = str(header_client_name)

        response = session.post(api_url, headers=headers, json=payload, timeout=30)
        if response.status_code == 429:
            raise RuntimeError(f"innertube_{endpoint_name}_rate_limited: HTTP 429")
        response.raise_for_status()
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise RuntimeError(f"innertube_{endpoint_name}_response_not_json")
        return response_payload

    def _fetch_next_response_with_innertube_sync(self, video_url: str, session: Any, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        video_id = self.extract_video_id(video_url)
        if not video_id:
            raise ValueError("missing_video_id")
        config = config or self._fetch_innertube_config_sync(session)
        payload = {
            "context": self._innertube_context(config),
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }
        return self._innertube_post_sync(
            session,
            config,
            "next",
            payload,
            referer=f"https://www.youtube.com/watch?v={video_id}",
            endpoint_override=os.environ.get("YOUTUBE_INNERTUBE_NEXT_URL") or "",
        )

    def _segments_from_transcript_api(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        segments = []
        renderer_keys = ("transcriptSegmentRenderer", "transcriptCueRenderer")
        for renderer_key in renderer_keys:
            for renderer in self._iter_dict_values(payload, renderer_key):
                if not isinstance(renderer, dict):
                    continue
                text = self._text_from_runs(
                    renderer.get("snippet")
                    or renderer.get("cue")
                    or renderer.get("text")
                    or renderer.get("body")
                )
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                start = (
                    renderer.get("startMs")
                    or renderer.get("startTimeMs")
                    or renderer.get("startOffsetMs")
                )
                duration = renderer.get("durationMs")
                end = renderer.get("endMs") or renderer.get("endTimeMs")
                try:
                    start_ms = int(start) if start is not None else None
                except (TypeError, ValueError):
                    start_ms = None
                try:
                    duration_ms = int(duration) if duration is not None else None
                except (TypeError, ValueError):
                    duration_ms = None
                if duration_ms is None and start_ms is not None and end is not None:
                    try:
                        duration_ms = max(0, int(end) - start_ms)
                    except (TypeError, ValueError):
                        duration_ms = None
                segments.append({
                    "start_ms": start_ms,
                    "duration_ms": duration_ms,
                    "text": text,
                })
            if segments:
                break
        return segments

    def _extract_transcript_with_client_sync(self, video_url: str, session: Any) -> Dict[str, Any]:
        video_id = self.extract_video_id(video_url)
        if not video_id:
            return self._transcript_defaults(
                "transcript_api_missing_video_id",
                status="unavailable",
                source="youtube_transcript_api",
            )

        circuit_result = self._transcript_circuit_result()
        if circuit_result:
            return circuit_result

        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import (
                AgeRestricted,
                InvalidVideoId,
                IpBlocked,
                NoTranscriptFound,
                PoTokenRequired,
                RequestBlocked,
                TranscriptsDisabled,
                VideoUnavailable,
                VideoUnplayable,
                YouTubeDataUnparsable,
                YouTubeRequestFailed,
            )
        except ImportError as exc:
            return self._transcript_defaults(
                f"transcript_dependency_missing: {self._compact_transcript_error(exc)}",
                status="dependency_missing",
                source="youtube_transcript_api",
            )

        max_attempts = self._env_int("YOUTUBE_TRANSCRIPT_RETRIES", 1) + 1
        retry_backoff = self._env_float("YOUTUBE_TRANSCRIPT_RETRY_BACKOFF_SECONDS", 2.0)
        last_error = "transcript_api_failed"
        last_status = "request_failed"

        for attempt in range(1, max_attempts + 1):
            circuit_result = self._transcript_circuit_result()
            if circuit_result:
                return circuit_result

            self._wait_for_transcript_slot()
            circuit_result = self._transcript_circuit_result()
            if circuit_result:
                return circuit_result

            try:
                session.headers.update(self._youtube_headers(accept="text/html,*/*"))
                client = YouTubeTranscriptApi(http_client=session)
                transcript_list = client.list(video_id)
                tracks = list(transcript_list)
                if not tracks:
                    self._register_transcript_result("unavailable")
                    return self._transcript_defaults(
                        "no_transcript_tracks",
                        status="unavailable",
                        source="youtube_transcript_api",
                        attempts=attempt,
                    )

                preferences = self._preferred_transcript_languages()

                def track_rank(track: Any) -> tuple:
                    language_code = str(getattr(track, "language_code", "") or "").lower()
                    language_rank = 1000
                    for index, preferred in enumerate(preferences):
                        if language_code == preferred:
                            language_rank = index * 10
                            break
                        if language_code.startswith(f"{preferred}-"):
                            language_rank = index * 10 + 1
                            break
                    generated_rank = 1 if bool(getattr(track, "is_generated", False)) else 0
                    return language_rank, generated_rank, language_code

                selected = sorted(tracks, key=track_rank)[0]
                fetched = selected.fetch()
                raw_segments = fetched.to_raw_data()
                cleaned_segments = []
                for segment in raw_segments:
                    if not isinstance(segment, dict):
                        continue
                    text = re.sub(r"\s+", " ", str(segment.get("text") or "")).strip()
                    if text:
                        cleaned_segments.append(text)

                transcript = "\n".join(cleaned_segments).strip()
                if not transcript:
                    last_status = "protocol_error"
                    last_error = "transcript_api_empty"
                    if attempt < max_attempts:
                        if retry_backoff:
                            time.sleep(retry_backoff * (2 ** (attempt - 1)))
                        continue
                    break

                self._register_transcript_result("ok")
                return {
                    "transcript": transcript,
                    "transcript_available": True,
                    "transcript_language": str(getattr(fetched, "language_code", "") or ""),
                    "transcript_language_name": str(getattr(fetched, "language", "") or ""),
                    "transcript_is_auto_generated": bool(getattr(fetched, "is_generated", False)),
                    "transcript_segment_count": len(cleaned_segments),
                    "transcript_error": "",
                    "transcript_status": "ok",
                    "transcript_source": "youtube_transcript_api",
                    "transcript_attempt_count": attempt,
                }
            except (IpBlocked, RequestBlocked) as exc:
                status = "rate_limited" if isinstance(exc, IpBlocked) else "blocked"
                error = self._compact_transcript_error(exc)
                self._register_transcript_result(status, error)
                return self._transcript_defaults(
                    error,
                    status=status,
                    source="youtube_transcript_api",
                    attempts=attempt,
                )
            except PoTokenRequired as exc:
                error = self._compact_transcript_error(exc)
                self._register_transcript_result("po_token_required", error)
                return self._transcript_defaults(
                    error,
                    status="po_token_required",
                    source="youtube_transcript_api",
                    attempts=attempt,
                )
            except (
                AgeRestricted,
                InvalidVideoId,
                NoTranscriptFound,
                TranscriptsDisabled,
                VideoUnavailable,
                VideoUnplayable,
            ) as exc:
                error = self._compact_transcript_error(exc)
                self._register_transcript_result("unavailable", error)
                return self._transcript_defaults(
                    error,
                    status="unavailable",
                    source="youtube_transcript_api",
                    attempts=attempt,
                )
            except (YouTubeRequestFailed, YouTubeDataUnparsable) as exc:
                last_status = "protocol_error" if isinstance(exc, YouTubeDataUnparsable) else "request_failed"
                last_error = self._compact_transcript_error(exc)
            except Exception as exc:
                last_status = "request_failed"
                last_error = self._compact_transcript_error(exc)

            if attempt < max_attempts and retry_backoff:
                time.sleep(retry_backoff * (2 ** (attempt - 1)))

        self._register_transcript_result(last_status, last_error)
        return self._transcript_defaults(
            last_error,
            status=last_status,
            source="youtube_transcript_api",
            attempts=max_attempts,
        )

    def _extract_transcript_from_get_transcript_sync(self, video_url: str, session: Any) -> Dict[str, Any]:
        if not video_url:
            return self._transcript_defaults(
                "get_transcript_missing_video_url",
                status="unavailable",
                source="innertube_get_transcript",
            )
        config = self._fetch_innertube_config_sync(session)
        next_response = self._fetch_next_response_with_innertube_sync(video_url, session, config=config)
        transcript_endpoint = None
        for endpoint in self._iter_dict_values(next_response, "getTranscriptEndpoint"):
            if isinstance(endpoint, dict) and endpoint.get("params"):
                transcript_endpoint = endpoint
                break
        if not transcript_endpoint:
            return self._transcript_defaults(
                "get_transcript_endpoint_not_found",
                status="unavailable",
                source="innertube_get_transcript",
                attempts=1,
            )

        response = self._innertube_post_sync(
            session,
            config,
            "get_transcript",
            {
                "context": self._innertube_context(config),
                "params": transcript_endpoint.get("params"),
            },
            referer=video_url,
            endpoint_override=os.environ.get("YOUTUBE_INNERTUBE_TRANSCRIPT_URL") or "",
        )
        segments = self._segments_from_transcript_api(response)
        transcript = "\n".join(segment["text"] for segment in segments).strip()
        if not transcript:
            return self._transcript_defaults(
                "get_transcript_empty",
                status="protocol_error",
                source="innertube_get_transcript",
                attempts=1,
            )

        return {
            "transcript": transcript,
            "transcript_available": True,
            "transcript_language": "",
            "transcript_language_name": "YouTube transcript",
            "transcript_is_auto_generated": False,
            "transcript_segment_count": len(segments),
            "transcript_error": "",
            "transcript_status": "ok",
            "transcript_source": "innertube_get_transcript",
            "transcript_attempt_count": 1,
        }

    def _caption_track_name(self, track: Dict[str, Any]) -> str:
        name = track.get("name")
        if isinstance(name, dict):
            return self._text_from_runs(name).strip()
        return str(name or "").strip()

    def _transcript_track_rank(self, track: Dict[str, Any]) -> tuple:
        language_code = (track.get("languageCode") or "").lower()
        preferences = self._preferred_transcript_languages()
        language_rank = 1000
        for index, preferred in enumerate(preferences):
            if language_code == preferred:
                language_rank = index * 10
                break
            if language_code.startswith(f"{preferred}-"):
                language_rank = index * 10 + 1
                break
        auto_rank = 1 if track.get("kind") == "asr" else 0
        return language_rank, auto_rank, self._caption_track_name(track).lower()

    def _caption_url_as_json3(self, base_url: str) -> str:
        parsed = urlparse(base_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "fmt"
        ]
        query.append(("fmt", "json3"))
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _caption_url_with_format(self, base_url: str, caption_format: str) -> str:
        parsed = urlparse(base_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "fmt"
        ]
        if caption_format:
            query.append(("fmt", caption_format))
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _segments_from_json3(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        segments = []
        for event in payload.get("events", []):
            if not isinstance(event, dict):
                continue
            text = "".join(
                segment.get("utf8", "")
                for segment in event.get("segs", [])
                if isinstance(segment, dict)
            )
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            segments.append({
                "start_ms": event.get("tStartMs"),
                "duration_ms": event.get("dDurationMs"),
                "text": text,
            })
        return segments

    def _segments_from_xml(self, raw_text: str) -> List[Dict[str, Any]]:
        from xml.etree import ElementTree

        root = ElementTree.fromstring(raw_text)
        segments = []
        for node in root.iter():
            if node.tag not in {"text", "p", "s"}:
                continue
            text = "".join(node.itertext())
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            start = node.attrib.get("start") or node.attrib.get("t")
            duration = node.attrib.get("dur") or node.attrib.get("d")
            try:
                start_ms = int(float(start) * 1000) if start is not None else None
            except ValueError:
                start_ms = None
            try:
                duration_ms = int(float(duration) * 1000) if duration is not None else None
            except ValueError:
                duration_ms = None
            segments.append({
                "start_ms": start_ms,
                "duration_ms": duration_ms,
                "text": text,
            })
        return segments

    def _transcript_endpoint_method(self) -> str:
        value = os.environ.get("YOUTUBE_TRANSCRIPT_ENDPOINT_METHOD", "api").strip().lower()
        aliases = {
            "api": "api",
            "client": "api",
            "library": "api",
            "youtube_transcript_api": "api",
            "get_transcript": "get_transcript",
            "innertube": "get_transcript",
            "panel": "get_transcript",
            "timedtext": "timedtext",
            "caption": "timedtext",
            "watch": "timedtext",
            "auto": "auto",
        }
        if value not in aliases:
            self.logger.warning("Ignoring invalid YOUTUBE_TRANSCRIPT_ENDPOINT_METHOD=%r; using api", value)
            return "api"
        return aliases[value]

    def _extract_transcript_from_timedtext_sync(
        self,
        player_response: Dict[str, Any],
        session: Any,
        video_url: str,
    ) -> Dict[str, Any]:
        renderer = (
            player_response.get("captions", {})
            .get("playerCaptionsTracklistRenderer", {})
        )
        tracks = renderer.get("captionTracks") or []
        if not tracks:
            return self._transcript_defaults(
                "no_caption_tracks",
                status="unavailable",
                source="timedtext",
            )

        track = sorted(tracks, key=self._transcript_track_rank)[0]
        base_url = track.get("baseUrl") or ""
        if not base_url:
            return self._transcript_defaults(
                "caption_track_has_no_url",
                status="protocol_error",
                source="timedtext",
            )

        headers = self._youtube_headers(
            referer=video_url,
            accept="application/json,text/xml,application/xml,text/plain,*/*",
        )
        last_error = "caption_track_empty"
        segments = []
        attempts = 0
        for caption_format in ("json3", "srv3", ""):
            attempts += 1
            url = self._caption_url_with_format(base_url, caption_format)
            try:
                response = session.get(url, headers=headers, timeout=30)
                if response.status_code == 429:
                    last_error = f"caption_rate_limited: HTTP 429 for fmt={caption_format or 'default'}"
                    self._register_transcript_result("rate_limited", last_error)
                    return self._transcript_defaults(
                        last_error,
                        status="rate_limited",
                        source="timedtext",
                        attempts=attempts,
                    )
                if response.status_code in {401, 403}:
                    last_error = f"caption_access_blocked: HTTP {response.status_code} for fmt={caption_format or 'default'}"
                    status = "po_token_required" if response.status_code == 403 else "blocked"
                    self._register_transcript_result(status, last_error)
                    return self._transcript_defaults(
                        last_error,
                        status=status,
                        source="timedtext",
                        attempts=attempts,
                    )
                response.raise_for_status()
                if not response.content:
                    last_error = f"caption_response_empty: fmt={caption_format or 'default'}"
                    continue
                if caption_format == "json3":
                    segments = self._segments_from_json3(response.json())
                else:
                    segments = self._segments_from_xml(response.text)
                if segments:
                    break
                last_error = f"caption_track_empty: fmt={caption_format or 'default'}"
            except Exception as exc:
                last_error = f"caption_fetch_failed: {self._compact_transcript_error(exc)}"

        transcript = "\n".join(segment["text"] for segment in segments).strip()
        if not transcript:
            self._register_transcript_result("protocol_error", last_error)
            return self._transcript_defaults(
                last_error,
                status="protocol_error",
                source="timedtext",
                attempts=attempts,
            )

        self._register_transcript_result("ok")
        return {
            "transcript": transcript,
            "transcript_available": True,
            "transcript_language": track.get("languageCode") or "",
            "transcript_language_name": self._caption_track_name(track),
            "transcript_is_auto_generated": track.get("kind") == "asr",
            "transcript_segment_count": len(segments),
            "transcript_error": "",
            "transcript_status": "ok",
            "transcript_source": "timedtext",
            "transcript_attempt_count": attempts,
        }

    def _extract_transcript_from_player_sync(self, player_response: Dict[str, Any], session: Any, video_url: str = "") -> Dict[str, Any]:
        if not self._transcripts_enabled():
            return self._transcript_defaults("disabled", status="disabled")

        endpoint_method = self._transcript_endpoint_method()
        errors = []
        if endpoint_method in {"api", "auto"}:
            result = self._extract_transcript_with_client_sync(video_url, session)
            if result.get("transcript_available"):
                return result
            errors.append(result.get("transcript_error") or "transcript_api_unavailable")
            if endpoint_method == "api" or result.get("transcript_status") in {
                "unavailable",
                "rate_limited",
                "blocked",
                "po_token_required",
                "circuit_open",
            }:
                return result

        if endpoint_method in {"get_transcript", "auto"}:
            try:
                result = self._extract_transcript_from_get_transcript_sync(video_url, session)
            except Exception as exc:
                error = f"get_transcript_failed: {self._compact_transcript_error(exc)}"
                status = "rate_limited" if "429" in error else "protocol_error"
                self._register_transcript_result(status, error)
                result = self._transcript_defaults(
                    error,
                    status=status,
                    source="innertube_get_transcript",
                    attempts=1,
                )
            if result.get("transcript_available"):
                return result
            errors.append(result.get("transcript_error") or "get_transcript_unavailable")
            if endpoint_method == "get_transcript" or result.get("transcript_status") == "rate_limited":
                return result

        result = self._extract_transcript_from_timedtext_sync(player_response, session, video_url)
        if result.get("transcript_available"):
            return result
        if errors:
            result["transcript_error"] = "; ".join([*errors, result.get("transcript_error") or "timedtext_unavailable"])
        return result

    def _extract_video_metadata_sync(self, video_data: Dict[str, Any], session: Any) -> Dict[str, Any]:
        video_url = self._watch_url_for_video(video_data)
        player_response: Dict[str, Any] = {}
        metadata_error = ""
        try:
            player_response = self._fetch_player_response_sync(video_url, session)
        except Exception as exc:
            metadata_error = f"player_response_failed: {self._compact_transcript_error(exc)}"

        details = player_response.get("videoDetails") or {}
        microformat = (
            player_response.get("microformat", {})
            .get("playerMicroformatRenderer", {})
        )
        thumbnails = details.get("thumbnail", {}).get("thumbnails", []) if isinstance(details.get("thumbnail"), dict) else []
        thumbnail_url = thumbnails[-1].get("url", "") if thumbnails and isinstance(thumbnails[-1], dict) else ""
        engagement_error = ""
        next_engagement: Dict[str, Any] = {}
        engagement_enabled = os.environ.get("YOUTUBE_ENGAGEMENT_API_ENABLED", "1").strip().lower() not in {
            "0", "false", "no", "off"
        }
        if engagement_enabled:
            try:
                next_response = self._fetch_next_response_with_innertube_sync(video_url, session)
                next_engagement = self._extract_next_engagement(next_response)
            except Exception as exc:
                engagement_error = f"next_response_failed: {self._compact_transcript_error(exc)}"

        fallback_subscribers = self._compact_public_count(video_data.get("subscriber_count"))
        like_count = next_engagement.get("like_count")
        follower_count = next_engagement.get("follower_count")
        if follower_count is None:
            follower_count = fallback_subscribers
        raw_views = details.get("viewCount")
        if raw_views in (None, ""):
            raw_views = video_data.get("views")
        metric_availability = {
            "views": "available" if raw_views not in (None, "") else "missing_from_public_response",
            "likes": (
                "available"
                if like_count is not None
                else "collection_failed"
                if engagement_error
                else "not_publicly_exposed"
            ),
            "shares": "not_publicly_exposed",
            "saves": "not_publicly_exposed",
            "followers": (
                "available" if follower_count is not None else "missing_from_public_response"
            ),
        }
        return {
            "title": details.get("title") or video_data.get("title") or "",
            "description": details.get("shortDescription") or "",
            "username": details.get("author") or microformat.get("ownerChannelName") or "",
            "creator_id": details.get("channelId") or microformat.get("externalChannelId") or "",
            "published": microformat.get("publishDate") or video_data.get("published") or "",
            "views": str(raw_views) if raw_views not in (None, "") else "",
            "like_count": like_count,
            "follower_count": follower_count,
            "metric_availability": metric_availability,
            "engagement_error": engagement_error,
            "duration_seconds": details.get("lengthSeconds") or 0,
            "thumbnail_url": thumbnail_url,
            "content_language": details.get("defaultAudioLanguage") or details.get("defaultLanguage") or "",
            "keywords": details.get("keywords") if isinstance(details.get("keywords"), list) else [],
            "category": microformat.get("category") or "",
            "is_live_content": details.get("isLiveContent"),
            "metadata_error": metadata_error,
            **self._extract_transcript_from_player_sync(player_response, session, video_url=video_url),
        }

    def _channel_url_from_endpoint(self, endpoint: Dict[str, Any], channel_id: str = "") -> str:
        if not isinstance(endpoint, dict):
            endpoint = {}
        browse = endpoint.get("browseEndpoint") if isinstance(endpoint, dict) else {}
        metadata = endpoint.get("commandMetadata", {}) if isinstance(endpoint, dict) else {}
        web_metadata = metadata.get("webCommandMetadata", {}) if isinstance(metadata, dict) else {}
        path = (
            (browse or {}).get("canonicalBaseUrl")
            or web_metadata.get("url")
            or (f"/channel/{channel_id}" if channel_id else "")
        )
        if not path:
            return ""
        return (path if path.startswith("http") else f"https://www.youtube.com{path}").rstrip("/")

    def _parse_video_renderers(
        self,
        data: Dict[str, Any],
        seen: set,
        *,
        default_channel_name: str = "",
        source: str = "search",
    ) -> List[Dict[str, Any]]:
        videos = []
        renderer_specs = (
            ("videoRenderer", "video"),
            ("gridVideoRenderer", "video"),
            ("reelItemRenderer", "short"),
        )
        for renderer_key, renderer_type in renderer_specs:
            for renderer in self._iter_dict_values(data, renderer_key):
                video_id = renderer.get("videoId")
                if not video_id or video_id in seen:
                    continue

                seen.add(video_id)
                title = self._text_from_runs(
                    renderer.get("title")
                    or renderer.get("headline")
                    or renderer.get("accessibilityText")
                ).strip()
                channel_name = self._text_from_runs(
                    renderer.get("ownerText")
                    or renderer.get("longBylineText")
                    or renderer.get("shortBylineText")
                ).strip()
                if not channel_name:
                    channel_name = default_channel_name or "Unknown Channel"

                videos.append({
                    "video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": title,
                    "username": channel_name,
                    "published": self._text_from_runs(renderer.get("publishedTimeText")).strip(),
                    "views": self._text_from_runs(renderer.get("viewCountText")).strip(),
                    "youtube_source": source,
                    "youtube_renderer": renderer_type,
                })

        for renderer in self._iter_dict_values(data, "lockupViewModel"):
            if not isinstance(renderer, dict):
                continue
            command = (
                renderer.get("rendererContext", {})
                .get("commandContext", {})
                .get("onTap", {})
                .get("innertubeCommand", {})
            )
            video_id = renderer.get("contentId") or command.get("watchEndpoint", {}).get("videoId")
            if not video_id or video_id in seen:
                continue

            metadata = renderer.get("metadata", {}).get("lockupMetadataViewModel", {})
            title_obj = metadata.get("title", {})
            title = (
                title_obj.get("content")
                or self._text_from_runs(title_obj)
                or renderer.get("rendererContext", {}).get("accessibilityContext", {}).get("label", "")
            ).strip()

            published = ""
            views = ""
            content_metadata = metadata.get("metadata", {}).get("contentMetadataViewModel", {})
            for row in content_metadata.get("metadataRows", []):
                for part in row.get("metadataParts", []):
                    text = part.get("text", {}).get("content", "") if isinstance(part, dict) else ""
                    label = part.get("accessibilityLabel", "") if isinstance(part, dict) else ""
                    value = label or text
                    lower_value = value.lower()
                    if "view" in lower_value or "ditonton" in lower_value:
                        views = value
                    elif value and not published:
                        published = value

            seen.add(video_id)
            videos.append({
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": title,
                "username": default_channel_name or "Unknown Channel",
                "published": published,
                "views": views,
                "youtube_source": source,
                "youtube_renderer": "lockup",
            })

        return videos

    def _parse_search_videos(self, data: Dict[str, Any], seen: set) -> List[Dict[str, Any]]:
        return self._parse_video_renderers(data, seen, source="search")

    def _parse_search_channels(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        channels = []
        seen: set[str] = set()
        for renderer in self._iter_dict_values(data, "channelRenderer"):
            channel_id = renderer.get("channelId") or ""
            title = self._text_from_runs(renderer.get("title")).strip()
            endpoint = renderer.get("navigationEndpoint") or {}
            url = self._channel_url_from_endpoint(endpoint, channel_id=channel_id)
            if not url:
                continue
            key = channel_id or url
            if key in seen:
                continue
            seen.add(key)
            channels.append({
                "channel_id": channel_id,
                "title": title,
                "url": url.rstrip("/"),
                "subscriber_count": self._text_from_runs(renderer.get("subscriberCountText")).strip(),
                "video_count": self._text_from_runs(renderer.get("videoCountText")).strip(),
            })

        for renderer in self._iter_dict_values(data, "videoRenderer"):
            owner_text = renderer.get("ownerText") or renderer.get("longBylineText") or renderer.get("shortBylineText")
            if not isinstance(owner_text, dict):
                continue
            for run in owner_text.get("runs", []):
                if not isinstance(run, dict):
                    continue
                title = (run.get("text") or "").strip()
                endpoint = run.get("navigationEndpoint") or {}
                browse = endpoint.get("browseEndpoint", {}) if isinstance(endpoint, dict) else {}
                channel_id = browse.get("browseId") or ""
                url = self._channel_url_from_endpoint(endpoint, channel_id=channel_id)
                if not title or not url:
                    continue
                key = channel_id or url
                if key in seen:
                    continue
                seen.add(key)
                channels.append({
                    "channel_id": channel_id,
                    "title": title,
                    "url": url,
                    "subscriber_count": "",
                    "video_count": "",
                    "source": "video_owner",
                })
        return channels

    def _best_channel_match(self, data: Dict[str, Any], query: str) -> Optional[Dict[str, Any]]:
        query_key = self._normalized_label(query)
        if not query_key:
            return None

        best = None
        best_score = 0
        for channel in self._parse_search_channels(data):
            title_key = self._normalized_label(channel.get("title", ""))
            url_key = self._normalized_label(channel.get("url", ""))
            if not title_key and not url_key:
                continue

            score = 0
            if title_key == query_key:
                score = 100
            elif self._near_label(title_key, query_key, max_distance=1):
                score = 95
            elif query_key in title_key or title_key in query_key:
                score = 85
            elif query_key in url_key:
                score = 80

            if score > best_score:
                best = channel
                best_score = score

        if best and best_score >= 80:
            best["match_score"] = best_score
            return best
        return None

    def _fetch_channel_tab_videos(
        self,
        downloader,
        channel: Dict[str, Any],
        tab: str,
        seen: set,
        max_videos: int = 0,
        max_pages: int = 100,
    ) -> List[Dict[str, Any]]:
        from youtube_comment_downloader.downloader import (
            YT_CFG_RE,
            YT_INITIAL_DATA_RE,
        )

        base_url = (channel.get("url") or "").rstrip("/")
        if not base_url:
            return []

        tab_url = f"{base_url}/{tab}"
        response = downloader.session.get(tab_url, timeout=30)
        if response.status_code >= 400:
            return []

        html = response.text
        ytcfg = json.loads(downloader.regex_search(html, YT_CFG_RE, default="{}"))
        data = json.loads(downloader.regex_search(html, YT_INITIAL_DATA_RE, default="{}"))
        if not ytcfg or not data:
            return []

        videos = self._parse_video_renderers(
            data,
            seen,
            default_channel_name=channel.get("title", ""),
            source=f"channel/{tab}",
        )
        continuation = self._find_search_continuation(data)

        page_number = 1
        while continuation and page_number < max_pages:
            if max_videos > 0 and len(seen) >= max_videos:
                break

            page_number += 1
            data = downloader.ajax_request(
                continuation,
                ytcfg,
                retries=2,
                sleep=1,
                timeout=30,
            ) or {}
            page_videos = self._parse_video_renderers(
                data,
                seen,
                default_channel_name=channel.get("title", ""),
                source=f"channel/{tab}",
            )
            if not page_videos:
                break

            videos.extend(page_videos)
            continuation = self._find_search_continuation(data)

        return videos

    def _channel_inventory_with_innertube(self, downloader, channel: Dict[str, Any], max_videos: int = 0) -> List[Dict[str, Any]]:
        max_pages = int(os.environ.get("YOUTUBE_CHANNEL_MAX_PAGES", "150") or "150")
        tabs = [
            tab.strip()
            for tab in os.environ.get("YOUTUBE_CHANNEL_TABS", "videos,streams,shorts").split(",")
            if tab.strip()
        ]
        videos = []
        seen: set[str] = set()
        for tab in tabs:
            tab_videos = self._fetch_channel_tab_videos(
                downloader,
                channel,
                tab,
                seen,
                max_videos=max_videos,
                max_pages=max_pages,
            )
            videos.extend(tab_videos)
            self.logger.info(
                "Channel tab %s for %s produced %s videos; channel total=%s",
                tab,
                channel.get("title") or channel.get("url"),
                len(tab_videos),
                len(videos),
            )
            if max_videos > 0 and len(videos) >= max_videos:
                return videos[:max_videos]
        return videos

    def _find_search_continuation(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        endpoints = []
        seen_tokens = set()

        for endpoint in self._iter_dict_values(data, "continuationEndpoint"):
            command = endpoint.get("continuationCommand", {}) if isinstance(endpoint, dict) else {}
            metadata = endpoint.get("commandMetadata", {}) if isinstance(endpoint, dict) else {}
            web_metadata = metadata.get("webCommandMetadata", {}) if isinstance(metadata, dict) else {}
            token = command.get("token")
            api_url = web_metadata.get("apiUrl")

            if token and api_url and token not in seen_tokens:
                endpoints.append(endpoint)
                seen_tokens.add(token)

        return endpoints[-1] if endpoints else None

    def _search_with_innertube(self, query: str, max_videos: int = 0, max_pages: int = 25) -> List[Dict[str, Any]]:
        """Search YouTube using InnerTube search continuations."""
        from youtube_comment_downloader.downloader import (
            YoutubeCommentDownloader,
            YT_CFG_RE,
            YT_INITIAL_DATA_RE,
        )

        downloader = YoutubeCommentDownloader()
        search_url = f"https://www.youtube.com/results?search_query={quote(query)}"
        response = downloader.session.get(search_url, timeout=30)
        response.raise_for_status()

        html = response.text
        ytcfg = json.loads(downloader.regex_search(html, YT_CFG_RE, default="{}"))
        data = json.loads(downloader.regex_search(html, YT_INITIAL_DATA_RE, default="{}"))
        if not ytcfg or not data:
            return []

        # Disabled exact channel match overriding search results to allow multi-account discovery
        # channel = self._best_channel_match(data, query)
        # if channel:
        #     channel_videos = self._channel_inventory_with_innertube(downloader, channel, max_videos=max_videos)
        #     if channel_videos:
        #         self.logger.info(
        #             "Exact channel match for query '%s': %s (%s). Channel inventory found %s videos.",
        #             query,
        #             channel.get("title"),
        #             channel.get("url"),
        #             len(channel_videos),
        #         )
        #         return channel_videos

        videos = []
        seen: set[str] = set()
        videos.extend(self._parse_search_videos(data, seen))
        continuation = self._find_search_continuation(data)

        page_number = 1
        while continuation and page_number < max_pages:
            if max_videos > 0 and len(videos) >= max_videos:
                break

            page_number += 1
            data = downloader.ajax_request(
                continuation,
                ytcfg,
                retries=2,
                sleep=1,
                timeout=30,
            ) or {}
            page_videos = self._parse_search_videos(data, seen)
            if not page_videos:
                break

            videos.extend(page_videos)
            continuation = self._find_search_continuation(data)

        if max_videos > 0:
            videos = videos[:max_videos]

        return videos

    async def search(self, query: str, max_videos: int = 10, context: Optional[BrowserContext] = None) -> List[Dict[str, Any]]:
        from ..raw_contract import transport_mode

        self.logger.info(f"Searching YouTube for '{query}'...")
        api_only = transport_mode() == "api-only"
        if self.is_channel_url(query):
            videos = await asyncio.to_thread(
                self._channel_inventory_from_url_sync,
                query,
                max_videos,
            )
            for video in videos:
                video.setdefault("discovery_method", "youtube_innertube_api")
                video.setdefault("metadata_method", "youtube_innertube_api")
            self.logger.info("Direct YouTube channel inventory found %s videos", len(videos))
            return videos
        try:
            videos = await asyncio.to_thread(self._search_with_innertube, query, max_videos)
            if videos:
                for video in videos:
                    video.setdefault("discovery_method", "youtube_innertube_api")
                    video.setdefault("metadata_method", "youtube_innertube_api")
                self.logger.info(f"InnerTube search found {len(videos)} videos for query '{query}'")
                return videos
            if api_only:
                raise RuntimeError("YouTube API-only search returned no videos")
            self.logger.warning("InnerTube search returned no videos; falling back to browser DOM search")
        except Exception as e:
            if api_only:
                raise
            self.logger.warning(f"InnerTube search failed, falling back to browser DOM search: {e}")

        manage_context = False
        if context is None:
            manage_context = True
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

        page = await context.new_page()
        try:
            url = f"https://www.youtube.com/results?search_query={quote(query)}"
            await page.goto(url)

            videos = []
            await page.wait_for_selector("ytd-video-renderer", timeout=10000)

            # Scroll a few times to load videos
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 1000)")
                await asyncio.sleep(1)

            video_elements = await page.query_selector_all("ytd-video-renderer")
            for el in video_elements:
                if max_videos > 0 and len(videos) >= max_videos:
                    break

                title_el = await el.query_selector("a#video-title")
                if not title_el:
                    continue

                href = await title_el.get_attribute("href")
                title = await title_el.get_attribute("title") or await title_el.inner_text()

                channel_el = await el.query_selector("ytd-channel-name a")
                channel_name = await channel_el.inner_text() if channel_el else "Unknown Channel"

                if href and "/watch?v=" in href:
                    video_id = self.extract_video_id(href)
                    videos.append({
                        "video_id": video_id,
                        "url": f"https://www.youtube.com{href}",
                        "title": title.strip(),
                        "username": channel_name.strip(),
                        "discovery_method": "browser_dom_fallback",
                        "metadata_method": "browser_dom",
                        "fallback_used": True,
                    })

            self.logger.info(f"Found {len(videos)} videos for query '{query}'")
            return videos

        except Exception as e:
            self.logger.error(f"Error searching YouTube: {e}")
            return []
        finally:
            await page.close()
            if manage_context:
                await browser.close()
                await playwright.stop()

    def _extract_comments_sync(self, video_data: Dict[str, Any], max_comments: int = 0) -> Dict[str, Any]:
        video_url = self._watch_url_for_video(video_data)
        comments = []

        from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_RECENT
        import requests

        session = requests.Session()
        metadata = self._extract_video_metadata_sync(video_data, session)
        downloader = YoutubeCommentDownloader()
        comment_error = ""
        comment_limit_reached = False
        comment_iteration_completed = False

        try:
            generator = downloader.get_comments_from_url(video_url, sort_by=SORT_BY_RECENT)
            count = 0
            for comment in generator:
                comments.append({
                    "author": comment.get("author", ""),
                    "author_id": comment.get("channel", ""),
                    "text": comment.get("text", ""),
                    "likes_text": str(comment.get("votes", "0")),
                    "time": comment.get("time", ""),
                    "platform": "youtube",
                    "video_id": video_data.get("video_id"),
                    "comment_id": comment.get("cid", ""),
                    "reply_count": comment.get("replies", 0),
                    "is_reply": bool(comment.get("reply", False)),
                })
                count += 1
                if max_comments and max_comments > 0 and count >= max_comments:
                    comment_limit_reached = True
                    break
            else:
                comment_iteration_completed = True
        except Exception as exc:
            comment_error = str(exc)
            self.logger.warning(f"Could not extract YouTube comments from {video_url}: {exc}")

        return {
            "video_id": video_data.get("video_id"),
            "url": video_url,
            "title": metadata.get("title") or video_data.get("title"),
            "caption": metadata.get("title") or video_data.get("title"),
            "description": metadata.get("description") or video_data.get("description", ""),
            "username": metadata.get("username") or video_data.get("username", "YouTube Channel"),
            "published": metadata.get("published") or video_data.get("published", ""),
            "views": metadata.get("views") or video_data.get("views", ""),
            "like_count": metadata.get("like_count"),
            "follower_count": metadata.get("follower_count"),
            "metric_availability": metadata.get("metric_availability", {}),
            "creator_id": metadata.get("creator_id") or video_data.get("creator_id", ""),
            "duration_seconds": metadata.get("duration_seconds") or video_data.get("duration_seconds", 0),
            "thumbnail_url": metadata.get("thumbnail_url") or video_data.get("thumbnail_url", ""),
            "content_language": metadata.get("content_language") or video_data.get("content_language", ""),
            "keywords": metadata.get("keywords") or video_data.get("keywords", []),
            "category": metadata.get("category") or video_data.get("category", ""),
            "is_live_content": metadata.get("is_live_content") if metadata.get("is_live_content") is not None else video_data.get("is_live_content"),
            "metadata_error": metadata.get("metadata_error", ""),
            "engagement_error": metadata.get("engagement_error", ""),
            "transcript": metadata.get("transcript", ""),
            "transcript_available": bool(metadata.get("transcript_available")),
            "transcript_language": metadata.get("transcript_language", ""),
            "transcript_language_name": metadata.get("transcript_language_name", ""),
            "transcript_is_auto_generated": bool(metadata.get("transcript_is_auto_generated")),
            "transcript_segment_count": metadata.get("transcript_segment_count", 0),
            "transcript_error": metadata.get("transcript_error", ""),
            "transcript_status": metadata.get("transcript_status", ""),
            "transcript_source": metadata.get("transcript_source", ""),
            "transcript_attempt_count": metadata.get("transcript_attempt_count", 0),
            "comment_error": comment_error,
            "discovery_method": video_data.get("discovery_method", "direct_url"),
            "metadata_method": "youtube_watch_player_api",
            "comment_method": "youtube_internal_api",
            "comments_exhausted": bool(comment_iteration_completed and not comment_error),
            "comment_limit_reached": comment_limit_reached,
            "comments_seen_in_response": len(comments),
            "fallback_used": bool(video_data.get("fallback_used")),
            "comments": comments,
        }

    async def extract_comments(self, video_data: Dict[str, Any], max_comments: int = 100, context: Optional[BrowserContext] = None) -> Dict[str, Any]:
        video_url = video_data.get("url")
        self.logger.info(f"Extracting comments from YouTube video {video_url}...")
        error_text = ""

        try:
            result = await asyncio.to_thread(self._extract_comments_sync, video_data, max_comments)
            self.logger.info(f"Extracted {len(result.get('comments', []))} comments from {video_url}")
            return result
        except Exception as e:
            error_text = str(e)
            self.logger.error(f"Error extracting comments for {video_url}: {e}")

        return {
            "video_id": video_data.get("video_id"),
            "url": video_url,
            "title": video_data.get("title"),
            "caption": video_data.get("title"),
            "description": video_data.get("description", ""),
            "username": video_data.get("username", "YouTube Channel"),
            **self._transcript_defaults(error_text),
            "comment_error": error_text,
            "comments": [],
        }

    def format_output(self, raw_comments: List[Any]) -> List[Dict[str, Any]]:
        return raw_comments
