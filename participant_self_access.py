"""Verify participant-owned account access without exposing private values.

This first adapter supports Instagram. It connects to the isolated social
browser, asks Instagram for the currently authenticated account's editable
profile, and returns only an identity-match boolean plus names of populated
fields. No response body, cookie, or personal field value is written.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
from typing import Any


INSTAGRAM_SELF_FIELDS = (
    "username",
    "full_name",
    "biography",
    "external_url",
    "email",
    "phone_number",
    "country_code",
    "national_number",
    "gender",
    "birthday",
)


async def inspect_instagram_self_access(
    *,
    expected_username: str,
    cdp_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    expected = expected_username.strip().lstrip("@").casefold()
    if not expected:
        raise ValueError("expected_username is required")

    playwright = await async_playwright().start()
    page: Any = None
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("The social browser has no active context")
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto(
            "https://www.instagram.com/accounts/edit/",
            wait_until="domcontentloaded",
            timeout=int(max(5.0, timeout_seconds) * 1000),
        )
        result = await page.evaluate(
            """
            async ({fields}) => {
              const cookieValue = (name) => {
                const prefix = name + "=";
                for (const part of document.cookie.split(";")) {
                  const value = part.trim();
                  if (value.startsWith(prefix)) {
                    return decodeURIComponent(value.slice(prefix.length));
                  }
                }
                return "";
              };
              const fingerprint = async (value) => {
                const bytes = new TextEncoder().encode(value);
                const digest = await crypto.subtle.digest("SHA-256", bytes);
                return Array.from(new Uint8Array(digest))
                  .map((byte) => byte.toString(16).padStart(2, "0"))
                  .join("");
              };
              const endpoints = [
                "/api/v1/accounts/edit/web_form_data/",
                "/api/v1/accounts/current_user/?edit=true",
                "/api/v1/accounts/current_user/"
              ];
              const attempts = [];
              for (const endpoint of endpoints) {
                const response = await fetch(endpoint, {
                  credentials: "include",
                  headers: {
                    "accept": "application/json, text/plain, */*",
                    "x-asbd-id": "129477",
                    "x-csrftoken": cookieValue("csrftoken"),
                    "x-ig-app-id": "936619743392459",
                    "x-requested-with": "XMLHttpRequest"
                  }
                });
                let payload = {};
                try {
                  payload = await response.json();
                } catch (_) {}
                const user = (
                  payload && typeof payload.form_data === "object"
                    ? payload.form_data
                    : payload && typeof payload.user === "object"
                      ? payload.user
                      : payload
                );
                const present = fields.filter((field) => {
                  const value = user ? user[field] : null;
                  return (
                    value !== undefined
                    && value !== null
                    && String(value).trim() !== ""
                  );
                });
                const username = user && user.username
                  ? String(user.username).toLowerCase()
                  : "";
                attempts.push({
                  endpoint_path: endpoint.split("?")[0],
                  http_status: response.status,
                  account_identity_available: Boolean(username),
                  populated_field_count: present.length
                });
                if (response.ok && (username || present.length)) {
                  return {
                    endpoint_path: endpoint.split("?")[0],
                    http_status: response.status,
                    account_identity_available: Boolean(username),
                    account_identity_fingerprint: username
                      ? await fingerprint(username)
                      : "",
                    available_field_names: present,
                    attempts
                  };
                }
              }
              const last = attempts.length ? attempts[attempts.length - 1] : {};
              return {
                endpoint_path: last.endpoint_path || "",
                http_status: last.http_status || 0,
                account_identity_available: false,
                account_identity_fingerprint: "",
                available_field_names: [],
                attempts
              };
            }
            """,
            {
                "fields": list(INSTAGRAM_SELF_FIELDS),
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("Instagram returned an invalid self-access summary")
        available = result.get("available_field_names")
        if not isinstance(available, list):
            available = []
        attempts = result.get("attempts")
        if not isinstance(attempts, list):
            attempts = []
        returned_fingerprint = str(
            result.get("account_identity_fingerprint") or ""
        )
        expected_fingerprint = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        account_match = bool(returned_fingerprint) and hmac.compare_digest(
            returned_fingerprint,
            expected_fingerprint,
        )
        return {
            "platform": "instagram",
            "access_scope": "authenticated_account_owner",
            "endpoint_path": result.get("endpoint_path"),
            "http_status": result.get("http_status"),
            "account_identity_available": bool(
                result.get("account_identity_available")
            ),
            "account_match": account_match,
            "identity_check_location": "local_python_sha256",
            "expected_identifier_sent_to_platform": False,
            "available_field_names": [
                str(field)
                for field in available
                if str(field) in INSTAGRAM_SELF_FIELDS
            ],
            "attempts": [
                {
                    "endpoint_path": str(item.get("endpoint_path") or ""),
                    "http_status": item.get("http_status"),
                    "account_identity_available": bool(
                        item.get("account_identity_available")
                    ),
                    "populated_field_count": int(
                        item.get("populated_field_count") or 0
                    ),
                }
                for item in attempts
                if isinstance(item, dict)
            ],
            "private_values_returned": False,
            "private_values_stored": False,
        }
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        await playwright.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a participant-owned Instagram session matches a "
            "consented profile and inventory available fields without values."
        )
    )
    parser.add_argument("--platform", choices=("instagram",), default="instagram")
    parser.add_argument("--expected-username", required=True)
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223")
    parser.add_argument("--timeout", type=float, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(
            inspect_instagram_self_access(
                expected_username=args.expected_username,
                cdp_url=args.cdp_url,
                timeout_seconds=max(5.0, args.timeout),
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "platform": args.platform,
                    "status": "error",
                    "error": str(exc),
                    "private_values_returned": False,
                    "private_values_stored": False,
                },
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, indent=2))
    if result.get("http_status") != 200:
        return 1
    return 0 if result.get("account_match") else 3


if __name__ == "__main__":
    raise SystemExit(main())
