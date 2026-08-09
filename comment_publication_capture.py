#!/usr/bin/env python
"""Capture comment-publication request shapes from the designated social browser.

The operator performs one legitimate comment manually while this recorder is
attached. It stores endpoint structure and redacted request/response templates,
never cookies, authorization values, or replayable anti-CSRF/signature values.
It does not submit or replay any request.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Request, Response, async_playwright

from tiktok_scraper.analysis_workflow import ensure_analysis_schema


WRITE_HINTS = (
    "comment",
    "reply",
    "feedback",
    "create",
    "publish",
    "thread",
)
NOISE_HINTS = (
    "analytics",
    "telemetry",
    "log_event",
    "performance",
    "client_event",
)
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:auth|bearer|cookie|csrf|device.?id|jazoest|lsd|mstoken|password|proof|"
    r"secret|session|signature|spin|token|x-bogus|x-gnarly|xsrf)",
    re.IGNORECASE,
)
SIGNATURE_KEY_PATTERN = re.compile(
    r"(?:csrf|jazoest|lsd|proof|signature|spin|token|x-bogus|x-gnarly)",
    re.IGNORECASE,
)
SAFE_HEADER_NAMES = {
    "accept",
    "accept-language",
    "content-type",
    "origin",
    "referer",
    "user-agent",
    "x-requested-with",
}


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower() or "capture"


def redact_url(url: str) -> tuple[str, list[str]]:
    parts = urlsplit(url)
    signature_fields: list[str] = []
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if SENSITIVE_KEY_PATTERN.search(key):
            query.append((key, "<redacted>"))
            signature_fields.append(key)
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)), signature_fields


def redact_headers(headers: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    output: dict[str, str] = {}
    signature_fields: list[str] = []
    for key, value in headers.items():
        lowered = key.casefold()
        if SENSITIVE_KEY_PATTERN.search(lowered):
            output[lowered] = "<redacted>"
            signature_fields.append(lowered)
        elif lowered in SAFE_HEADER_NAMES or lowered.startswith("sec-ch-"):
            output[lowered] = value
    return output, signature_fields


def redact_value(value: Any, path: str = "") -> tuple[Any, list[str]]:
    signature_fields: list[str] = []
    if isinstance(value, dict):
        output = {}
        for key, child in value.items():
            child_path = f"{path}.{key}".strip(".")
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                output[key] = "<redacted>"
                signature_fields.append(child_path)
            else:
                output[key], child_fields = redact_value(child, child_path)
                signature_fields.extend(child_fields)
        return output, signature_fields
    if isinstance(value, list):
        output = []
        for index, child in enumerate(value):
            redacted, child_fields = redact_value(child, f"{path}[{index}]")
            output.append(redacted)
            signature_fields.extend(child_fields)
        return output, signature_fields
    return value, signature_fields


def request_body_template(post_data: str | None, content_type: str) -> tuple[Any, list[str]]:
    if not post_data:
        return "", []
    if "json" in content_type.casefold() or post_data.lstrip().startswith(("{", "[")):
        try:
            return redact_value(json.loads(post_data))
        except json.JSONDecodeError:
            pass
    if "form" in content_type.casefold() or "=" in post_data:
        try:
            values: dict[str, Any] = {}
            signature_fields: list[str] = []
            for key, value in parse_qsl(post_data, keep_blank_values=True):
                if SENSITIVE_KEY_PATTERN.search(key):
                    values[key] = "<redacted>"
                    signature_fields.append(key)
                else:
                    values[key] = value
            return values, signature_fields
        except ValueError:
            pass
    return post_data[:20000], []


def looks_like_comment_write(method: str, url: str, post_data: str | None = None) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH"}:
        return False
    haystack = f"{url}\n{post_data or ''}".casefold()
    if any(hint in haystack for hint in NOISE_HINTS):
        return False
    return any(hint in haystack for hint in WRITE_HINTS)


def response_template(payload: Any) -> Any:
    redacted, _ = redact_value(payload)
    encoded = json.dumps(redacted, ensure_ascii=False)
    if len(encoded) <= 100000:
        return redacted
    if isinstance(redacted, dict):
        return {"keys": sorted(redacted.keys()), "truncated": True}
    return {"type": type(redacted).__name__, "truncated": True}


class PublicationCapture:
    def __init__(self, platform: str, target_url: str, project: str = "") -> None:
        self.platform = platform
        self.target_url = target_url
        self.project = project
        self.pending: dict[str, dict[str, Any]] = {}
        self.completed_request_keys: set[str] = set()
        self.records: list[dict[str, Any]] = []

    @staticmethod
    def _request_key(request: Request) -> str:
        implementation = getattr(request, "_impl_obj", None)
        guid = getattr(implementation, "_guid", "")
        return str(guid or id(request))

    async def on_request(self, request: Request) -> None:
        try:
            request_key = self._request_key(request)
            if request_key in self.completed_request_keys:
                return
            post_data = request.post_data
            if not looks_like_comment_write(request.method, request.url, post_data):
                return
            safe_url, url_fields = redact_url(request.url)
            safe_headers, header_fields = redact_headers(await request.all_headers())
            body, body_fields = request_body_template(
                post_data,
                safe_headers.get("content-type", ""),
            )
            signature_fields = sorted(set([
                *url_fields,
                *header_fields,
                *body_fields,
                *[
                    field
                    for field in [*url_fields, *header_fields, *body_fields]
                    if SIGNATURE_KEY_PATTERN.search(field)
                ],
            ]))
            captured_at = now_iso()
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "method": request.method,
                        "url": safe_url,
                        "body": body,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if request_key in self.completed_request_keys:
                return
            self.pending[request_key] = {
                "capture_id": fingerprint[:32],
                "project": self.project,
                "platform": self.platform,
                "target_url": self.target_url,
                "captured_at": captured_at,
                "request_url": safe_url,
                "method": request.method,
                "request_headers": safe_headers,
                "request_body_template": body,
                "response_status": None,
                "response": {},
                "signature_fields": signature_fields,
            }
        except Exception:
            return

    async def on_response(self, response: Response) -> None:
        request_key = self._request_key(response.request)
        if request_key in self.completed_request_keys:
            return
        record = self.pending.pop(request_key, None)
        if not record:
            await self.on_request(response.request)
            record = self.pending.pop(request_key, None)
        if not record:
            return
        record["response_status"] = response.status
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type.casefold():
                record["response"] = response_template(await response.json())
            else:
                text = await response.text()
                record["response"] = {"text": text[:20000]}
        except Exception as exc:
            record["response"] = {"capture_error": str(exc)}
        self.completed_request_keys.add(request_key)
        self.records.append(record)

    def flush_unanswered(self) -> None:
        self.records.extend(self.pending.values())
        self.pending.clear()


def write_capture_files(records: list[dict[str, Any]], output_dir: Path, platform: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"{slugify(platform)}_comment_publication_{timestamp}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "captured_at": now_iso(),
                "platform": platform,
                "contains_replayable_credentials": False,
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def store_captures(database: Path, records: list[dict[str, Any]]) -> int:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    ensure_analysis_schema(conn)
    stored = 0
    for record in records:
        stored += conn.execute(
            """
            INSERT OR REPLACE INTO publication_captures (
                capture_id, project, platform, target_url, captured_at,
                request_url, method, request_headers_json,
                request_body_template, response_status, response_json,
                signature_fields_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["capture_id"],
                record["project"],
                record["platform"],
                record["target_url"],
                record["captured_at"],
                record["request_url"],
                record["method"],
                json.dumps(record["request_headers"], ensure_ascii=False),
                json.dumps(record["request_body_template"], ensure_ascii=False),
                record["response_status"],
                json.dumps(record["response"], ensure_ascii=False),
                json.dumps(record["signature_fields"], ensure_ascii=False),
            ),
        ).rowcount
    conn.commit()
    conn.close()
    return stored


async def capture(args: argparse.Namespace) -> dict[str, Any]:
    recorder = PublicationCapture(args.platform, args.url, args.project)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(args.cdp_url)
        if not browser.contexts:
            raise RuntimeError("The social browser has no active context")
        context = browser.contexts[0]
        page = next((page for page in context.pages if args.url in page.url), None)
        if page is None:
            page = await context.new_page()
            await page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.on("request", recorder.on_request)
        page.on("response", recorder.on_response)
        print(
            f"[CAPTURE] {args.platform} is ready at {page.url}\n"
            f"[CAPTURE] Manually publish one approved test comment within {args.seconds} seconds.\n"
            "[CAPTURE] Credentials and replayable token values will be redacted.",
            flush=True,
        )
        await page.wait_for_timeout(max(1, args.seconds) * 1000)
        recorder.flush_unanswered()
        await browser.close()
    path = write_capture_files(recorder.records, Path(args.output_dir), args.platform)
    stored = store_captures(Path(args.database), recorder.records) if args.database else 0
    return {"records": len(recorder.records), "stored": stored, "output": str(path)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture and redact one manual comment-publication request."
    )
    parser.add_argument("--platform", choices=["youtube", "tiktok", "instagram", "facebook", "x"], required=True)
    parser.add_argument("--url", required=True, help="Approved post URL used for the test comment.")
    parser.add_argument("--project", default="")
    parser.add_argument("--database", default="", help="Optional project scrape_state.sqlite path.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument(
        "--output-dir",
        default=str(Path("comments_data") / "publication_captures"),
    )
    return parser.parse_args()


def main() -> None:
    result = asyncio.run(capture(parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
