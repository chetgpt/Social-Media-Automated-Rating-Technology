"""Owner-operated profile export with encrypted local storage.

The exporter reads only the account currently authenticated in the participant's
isolated browser. It does not accept or send a target username, and it does not
infer demographic or geographic attributes.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
from ctypes import wintypes
import datetime as dt
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from tiktok_scraper.profile_web_api import decode_json_payloads


EXPORT_FORMAT = "participant-owner-profile"
EXPORT_VERSION = 1
DPAPI_ENTROPY = b"participant-owner-profile-export-v1"
SUPPORTED_OWNER_PLATFORMS = ("instagram", "facebook", "tiktok", "x", "youtube")

OWNER_FIELDS = (
    "id",
    "pk",
    "uid",
    "user_id",
    "rest_id",
    "channel_id",
    "external_id",
    "gaia_id",
    "username",
    "unique_id",
    "uniqueid",
    "screen_name",
    "handle",
    "full_name",
    "display_name",
    "name",
    "title",
    "biography",
    "bio",
    "description",
    "external_url",
    "vanity_channel_url",
    "pronouns",
    "email",
    "email_address",
    "phone_number",
    "phone",
    "mobile_number",
    "country_code",
    "national_number",
    "public_email",
    "public_phone_country_code",
    "public_phone_number",
    "contact_phone_number",
    "gender",
    "birthday",
    "birth_date",
    "date_of_birth",
    "age",
    "account_type",
    "is_business",
    "is_professional_account",
    "category",
    "category_name",
    "business_address_json",
    "business_address",
    "address",
    "address_street",
    "street",
    "city_id",
    "city_name",
    "city",
    "state",
    "region",
    "country",
    "location",
    "zip",
    "postal_code",
    "latitude",
    "longitude",
    "timezone",
    "language",
)

FIELD_ALIASES = {
    "account_id": "account_id",
    "id": "account_id",
    "pk": "account_id",
    "uid": "account_id",
    "user_id": "account_id",
    "rest_id": "account_id",
    "channel_id": "account_id",
    "external_id": "account_id",
    "gaia_id": "account_id",
    "username": "username",
    "unique_id": "username",
    "uniqueid": "username",
    "screen_name": "username",
    "handle": "username",
    "full_name": "display_name",
    "display_name": "display_name",
    "name": "display_name",
    "title": "display_name",
    "biography": "biography",
    "bio": "biography",
    "description": "biography",
    "external_url": "external_url",
    "vanity_channel_url": "external_url",
    "pronouns": "pronouns",
    "kata_ganti": "pronouns",
    "email": "email",
    "email_address": "email",
    "surel": "email",
    "phone_number": "phone_number",
    "phone": "phone_number",
    "mobile_number": "phone_number",
    "telepon": "phone_number",
    "country_code": "phone_country_code",
    "national_number": "phone_national_number",
    "public_email": "public_email",
    "public_phone_country_code": "public_phone_country_code",
    "public_phone_number": "public_phone_number",
    "contact_phone_number": "contact_phone_number",
    "gender": "gender",
    "jenis_kelamin": "gender",
    "birthday": "birthday",
    "birth_date": "birthday",
    "date_of_birth": "birthday",
    "tanggal_lahir": "birthday",
    "age": "age",
    "account_type": "account_type",
    "is_business": "is_business",
    "is_professional_account": "is_professional_account",
    "category": "category",
    "category_name": "category",
    "business_address_json": "business_address",
    "business_address": "business_address",
    "address": "address",
    "alamat": "address",
    "address_street": "street",
    "street": "street",
    "city_id": "city_id",
    "city_name": "city",
    "city": "city",
    "kota": "city",
    "state": "state",
    "provinsi": "state",
    "region": "region",
    "country": "country",
    "negara": "country",
    "location": "location",
    "lokasi": "location",
    "zip": "postal_code",
    "postal_code": "postal_code",
    "kode_pos": "postal_code",
    "latitude": "latitude",
    "longitude": "longitude",
    "timezone": "timezone",
    "language": "language",
}

DEMOGRAPHIC_FIELDS = ("gender", "birthday", "age", "pronouns")
GEOGRAPHIC_FIELDS = (
    "business_address",
    "address",
    "street",
    "city_id",
    "city",
    "state",
    "region",
    "country",
    "location",
    "postal_code",
    "latitude",
    "longitude",
    "timezone",
)

CONTACT_FIELDS = (
    "email",
    "phone_number",
    "phone_country_code",
    "phone_national_number",
    "public_email",
    "public_phone_country_code",
    "public_phone_number",
    "contact_phone_number",
)

OWNER_SURFACES = {
    "instagram": {
        "urls": (
            "https://www.instagram.com/accounts/edit/",
            "https://accountscenter.instagram.com/personal_info/",
        ),
        "hosts": ("instagram.com",),
        "response_markers": (
            "/api/v1/accounts/",
            "/api/v1/users/web_profile_info/",
            "/api/graphql",
        ),
        "script_globals": (),
        "script_ids": (),
    },
    "facebook": {
        "urls": (
            "https://www.facebook.com/settings/?tab=profile",
            "https://accountscenter.facebook.com/personal_info/",
        ),
        "hosts": ("facebook.com",),
        "response_markers": ("/api/graphql", "/ajax/settings/"),
        "script_globals": (),
        "script_ids": (),
    },
    "tiktok": {
        "urls": ("https://www.tiktok.com/setting",),
        "hosts": ("tiktok.com",),
        "response_markers": (
            "/passport/web/account/",
            "/api/user/detail/",
            "/api/account/",
            "/api/self/",
        ),
        "script_globals": (
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
            "SIGI_STATE",
        ),
        "script_ids": (
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
            "SIGI_STATE",
        ),
    },
    "x": {
        "urls": (
            "https://x.com/settings/account",
            "https://x.com/settings/your_twitter_data/account",
        ),
        "hosts": ("x.com", "twitter.com"),
        "response_markers": (
            "/i/api/1.1/account/",
            "/i/api/graphql/",
            "/settings/",
        ),
        "script_globals": (),
        "script_ids": (),
    },
    "youtube": {
        "urls": (
            "https://www.youtube.com/account",
            "https://myaccount.google.com/personal-info",
        ),
        "hosts": ("youtube.com", "google.com"),
        "response_markers": (
            "/youtubei/v1/",
            "/batchexecute",
            "/personal-info",
        ),
        "script_globals": ("ytInitialData",),
        "script_ids": (),
    },
}

AUTH_COOKIE_RULES = {
    "instagram": {
        "urls": ("https://www.instagram.com/",),
        "required_all": ("sessionid",),
        "required_any": (),
    },
    "facebook": {
        "urls": ("https://www.facebook.com/",),
        "required_all": ("c_user", "xs"),
        "required_any": (),
    },
    "tiktok": {
        "urls": ("https://www.tiktok.com/",),
        "required_all": (),
        "required_any": ("sessionid", "sessionid_ss", "sid_tt"),
    },
    "x": {
        "urls": ("https://x.com/", "https://twitter.com/"),
        "required_all": ("auth_token", "ct0"),
        "required_any": (),
    },
    "youtube": {
        "urls": ("https://www.youtube.com/", "https://accounts.google.com/"),
        "required_all": (),
        "required_any": (
            "SAPISID",
            "__Secure-1PAPISID",
            "__Secure-3PAPISID",
            "LOGIN_INFO",
        ),
    },
}

BLOCKED_FIELD_PARTS = (
    "password",
    "passwd",
    "token",
    "cookie",
    "secret",
    "session",
    "csrf",
    "authorization",
    "auth_token",
)

VISIBLE_FIELD_LABELS = (
    "Birthday",
    "Date of birth",
    "Gender",
    "Pronouns",
    "Address",
    "Location",
    "City",
    "State",
    "Region",
    "Country",
    "Postal code",
    "Phone",
    "Email",
    "Tanggal lahir",
    "Jenis kelamin",
    "Kata ganti",
    "Alamat",
    "Lokasi",
    "Kota",
    "Provinsi",
    "Negara",
    "Kode pos",
    "Telepon",
    "Surel",
)

INSTAGRAM_OWNER_ENDPOINTS = (
    "/api/v1/accounts/edit/web_form_data/",
    "/api/v1/accounts/current_user/?edit=true",
    "/api/v1/accounts/current_user/",
)


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _dpapi_library() -> tuple[Any, Any]:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI protection is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    return crypt32, kernel32


def dpapi_protect(data: bytes) -> bytes:
    crypt32, kernel32 = _dpapi_library()
    input_blob, input_buffer = _input_blob(data)
    entropy_blob, entropy_buffer = _input_blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Participant owner profile",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _ = input_buffer, entropy_buffer
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    crypt32, kernel32 = _dpapi_library()
    input_blob, input_buffer = _input_blob(data)
    entropy_blob, entropy_buffer = _input_blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _ = input_buffer, entropy_buffer
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output_blob.pbData)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (bool, int, float, list, dict))


def normalize_field_name(value: Any) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def canonical_field_name(value: Any) -> str:
    normalized = normalize_field_name(value)
    if normalized in FIELD_ALIASES.values():
        return normalized
    return FIELD_ALIASES.get(normalized, "")


def _safe_field_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, list):
        safe = [
            _safe_field_value(item)
            for item in value[:50]
            if isinstance(item, (str, bool, int, float, dict))
        ]
        return safe
    if isinstance(value, dict):
        safe_dict: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            normalized_key = normalize_field_name(key)
            if any(part in normalized_key for part in BLOCKED_FIELD_PARTS):
                continue
            if isinstance(item, (str, bool, int, float, list, dict)):
                safe_dict[str(key)] = _safe_field_value(item)
        return safe_dict
    return str(value)


def select_owner_fields(profile: Mapping[str, Any]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    allowed = set(OWNER_FIELDS) | set(FIELD_ALIASES.values())
    for key, value in profile.items():
        normalized = normalize_field_name(key)
        if normalized not in allowed:
            continue
        if any(part in normalized for part in BLOCKED_FIELD_PARTS):
            continue
        if _has_value(value):
            selected[normalized] = _safe_field_value(value)
    return selected


def normalize_owner_fields(profile: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in select_owner_fields(profile).items():
        canonical = canonical_field_name(key)
        if canonical and canonical not in normalized:
            normalized[canonical] = value
    return normalized


def build_owner_export(
    *,
    platform: str,
    profile: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    exported_at: str | None = None,
) -> dict[str, Any]:
    selected = select_owner_fields(profile)
    normalized = normalize_owner_fields(selected)
    return {
        "schema": f"{EXPORT_FORMAT}/{EXPORT_VERSION}",
        "platform": platform,
        "access_scope": "authenticated_account_owner",
        "exported_at": exported_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        "inference_used": False,
        "owner_profile": selected,
        "normalized_owner_profile": normalized,
        "demographics": {
            field: normalized[field]
            for field in DEMOGRAPHIC_FIELDS
            if field in normalized
        },
        "geography": {
            field: normalized[field]
            for field in GEOGRAPHIC_FIELDS
            if field in normalized
        },
        "contacts": {
            field: normalized[field]
            for field in CONTACT_FIELDS
            if field in normalized
        },
        "sources": [
            {
                "endpoint_path": str(source.get("endpoint_path") or ""),
                "http_status": int(source.get("http_status") or 0),
                "source_surface": str(source.get("source_surface") or ""),
            }
            for source in sources
        ],
    }


def write_encrypted_export(path: Path, payload: Mapping[str, Any]) -> None:
    plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "protection": "windows-dpapi-current-user",
        "ciphertext": base64.b64encode(dpapi_protect(plaintext)).decode("ascii"),
    }
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(envelope, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_encrypted_export(path: Path) -> dict[str, Any]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if (
        envelope.get("format") != EXPORT_FORMAT
        or envelope.get("version") != EXPORT_VERSION
        or envelope.get("protection") != "windows-dpapi-current-user"
    ):
        raise ValueError("Unsupported participant profile export")
    ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    payload = json.loads(dpapi_unprotect(ciphertext).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Decrypted participant profile is not an object")
    return payload


def write_plaintext_export(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def owner_response_url_matches(platform: str, url: str) -> bool:
    config = OWNER_SURFACES.get(platform)
    if not config:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    allowed_host = any(
        host == domain or host.endswith(f".{domain}")
        for domain in config["hosts"]
    )
    if not allowed_host:
        return False
    lowered = url.casefold()
    return any(marker in lowered for marker in config["response_markers"])


def _mapping_owner_score(value: Mapping[str, Any]) -> int:
    canonical_names = {
        canonical_field_name(key)
        for key in value
        if canonical_field_name(key)
    }
    identity_names = {
        "account_id",
        "username",
        "email",
        "phone_number",
    }
    detail_names = set(DEMOGRAPHIC_FIELDS) | set(GEOGRAPHIC_FIELDS) | {
        "display_name",
        "biography",
        "account_type",
        "category",
    }
    identity_count = len(canonical_names & identity_names)
    if not identity_count:
        return 0
    return identity_count * 10 + len(canonical_names & detail_names) * 3 + len(
        canonical_names
    )


def extract_owner_fields_from_payload(payload: Any) -> dict[str, Any]:
    best_score = 0
    best_fields: dict[str, Any] = {}
    seen: set[int] = set()
    visited = 0

    def visit(value: Any, depth: int) -> None:
        nonlocal best_fields, best_score, visited
        visited += 1
        if visited > 100_000 or depth > 30:
            return
        if isinstance(value, list):
            for item in value[:500]:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        score = _mapping_owner_score(value)
        if score > best_score:
            selected = select_owner_fields(value)
            if selected:
                best_score = score
                best_fields = selected
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child, depth + 1)

    visit(payload, 0)
    return best_fields


def merge_owner_field_records(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for record in records:
        for key, value in select_owner_fields(record).items():
            if key not in merged and _has_value(value):
                merged[key] = value
    return merged


async def _context_is_authenticated(context: Any, platform: str) -> bool:
    rule = AUTH_COOKIE_RULES[platform]
    cookie_names: set[str] = set()
    try:
        cookies = await context.cookies(list(rule["urls"]))
    except Exception:
        return False
    cookie_names.update(str(item.get("name") or "") for item in cookies)
    required_all = set(rule["required_all"])
    required_any = set(rule["required_any"])
    if not required_all.issubset(cookie_names):
        return False
    return not required_any or bool(required_any & cookie_names)


async def owner_session_status(
    *,
    cdp_url: str,
    platforms: Sequence[str] = SUPPORTED_OWNER_PLATFORMS,
) -> dict[str, bool]:
    from playwright.async_api import async_playwright

    normalized = [
        platform.strip().casefold()
        for platform in platforms
        if platform.strip().casefold() in SUPPORTED_OWNER_PLATFORMS
    ]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("The participant browser has no active context")
        context = browser.contexts[0]
        values = await asyncio.gather(
            *(
                _context_is_authenticated(context, platform)
                for platform in normalized
            )
        )
    return dict(zip(normalized, values))


async def _capture_owner_response(
    response: Any,
    *,
    platform: str,
    field_records: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> None:
    if not owner_response_url_matches(platform, response.url):
        return
    path = urlparse(response.url).path
    source = {
        "endpoint_path": path,
        "http_status": int(response.status or 0),
    }
    sources.append(source)
    try:
        raw = await response.text()
    except Exception:
        return
    if len(raw) > 5_000_000:
        return
    for payload in decode_json_payloads(raw):
        fields = extract_owner_fields_from_payload(payload)
        if fields:
            field_records.append(fields)


async def _read_dom_owner_fields(page: Any) -> dict[str, Any]:
    values = await page.evaluate(
        """
        ({blockedParts, labels}) => {
          const blocked = (value) => {
            const normalized = String(value || "").toLowerCase();
            return blockedParts.some((part) => normalized.includes(part));
          };
          const controls = [];
          for (const element of document.querySelectorAll("input, select, textarea")) {
            const type = String(element.type || "").toLowerCase();
            if (type === "password" || type === "hidden") continue;
            const key = (
              element.name
              || element.getAttribute("aria-label")
              || element.id
              || element.autocomplete
              || ""
            );
            if (!key || blocked(key)) continue;
            const value = element.value;
            if (value === undefined || value === null || String(value).trim() === "") {
              continue;
            }
            controls.push({key, value: String(value)});
            if (controls.length >= 100) break;
          }

          const labelled = [];
          const wanted = new Set(labels.map((label) => label.toLowerCase()));
          for (const element of document.querySelectorAll(
            "label, dt, th, [role='heading'], [data-testid]"
          )) {
            const label = String(element.textContent || "").trim();
            if (!wanted.has(label.toLowerCase())) continue;
            let row = element.parentElement;
            for (let depth = 0; row && depth < 4; depth += 1) {
              const text = String(row.innerText || "").trim();
              if (text && text !== label && text.length <= 1000) {
                labelled.push({key: label, value: text});
                break;
              }
              row = row.parentElement;
            }
            if (labelled.length >= 40) break;
          }
          return {controls, labelled};
        }
        """,
        {
            "blockedParts": list(BLOCKED_FIELD_PARTS),
            "labels": list(VISIBLE_FIELD_LABELS),
        },
    )
    if not isinstance(values, dict):
        return {}
    selected: dict[str, Any] = {}
    for group_name in ("controls", "labelled"):
        group = values.get(group_name)
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            canonical = canonical_field_name(item.get("key"))
            value = item.get("value")
            if canonical and _has_value(value) and canonical not in selected:
                selected[canonical] = value
    return selected


async def _read_embedded_owner_fields(page: Any, platform: str) -> dict[str, Any]:
    config = OWNER_SURFACES[platform]
    roots = await page.evaluate(
        """
        ({globalNames, scriptIds}) => {
          const roots = [];
          for (const name of globalNames) {
            if (window[name] && typeof window[name] === "object") {
              roots.push(window[name]);
            }
          }
          for (const id of scriptIds) {
            const text = document.getElementById(id)?.textContent || "";
            if (!text || text.length > 5000000) continue;
            try { roots.push(JSON.parse(text)); } catch (_) {}
          }
          return roots.slice(0, 12);
        }
        """,
        {
            "globalNames": list(config["script_globals"]),
            "scriptIds": list(config["script_ids"]),
        },
    )
    if not isinstance(roots, list):
        return {}
    records = [
        extract_owner_fields_from_payload(root)
        for root in roots
        if isinstance(root, (dict, list))
    ]
    return merge_owner_field_records([record for record in records if record])


async def fetch_surface_owner_profile(
    *,
    platform: str,
    cdp_url: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from playwright.async_api import async_playwright

    if platform not in SUPPORTED_OWNER_PLATFORMS:
        raise ValueError(f"Unsupported owner profile platform: {platform}")
    config = OWNER_SURFACES[platform]
    playwright = await async_playwright().start()
    pages: list[Any] = []
    response_tasks: list[asyncio.Task[Any]] = []
    field_records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("The participant browser has no active context")
        context = browser.contexts[0]
        if not await _context_is_authenticated(context, platform):
            raise RuntimeError(
                f"The participant browser is not signed in to {platform}"
            )

        def handle_response(response: Any) -> None:
            if not owner_response_url_matches(platform, response.url):
                return
            task = asyncio.create_task(
                _capture_owner_response(
                    response,
                    platform=platform,
                    field_records=field_records,
                    sources=sources,
                )
            )
            response_tasks.append(task)

        for surface_url in config["urls"]:
            page = await context.new_page()
            pages.append(page)
            page.on("response", handle_response)
            try:
                await page.goto(
                    surface_url,
                    wait_until="domcontentloaded",
                    timeout=int(max(5.0, timeout_seconds) * 1000),
                )
                await page.wait_for_timeout(
                    int(min(3.0, max(1.0, timeout_seconds / 4)) * 1000)
                )
            except Exception:
                pass
            try:
                dom_fields = await _read_dom_owner_fields(page)
            except Exception:
                dom_fields = {}
            if dom_fields:
                field_records.insert(0, dom_fields)
                sources.append(
                    {
                        "endpoint_path": urlparse(surface_url).path,
                        "http_status": 200,
                        "source_surface": "owner_settings_dom",
                    }
                )
            try:
                embedded_fields = await _read_embedded_owner_fields(page, platform)
            except Exception:
                embedded_fields = {}
            if embedded_fields:
                field_records.append(embedded_fields)
                sources.append(
                    {
                        "endpoint_path": urlparse(surface_url).path,
                        "http_status": 200,
                        "source_surface": "owner_settings_embedded_data",
                    }
                )

        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        merged = merge_owner_field_records(field_records)
        if not merged:
            raise RuntimeError(
                f"No owner profile fields were exposed on {platform} account surfaces"
            )
        return merged, sources
    finally:
        for page in pages:
            try:
                await page.close()
            except Exception:
                pass
        await playwright.stop()


async def fetch_instagram_owner_profile(
    *,
    cdp_url: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    page: Any = None
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("The participant browser has no active context")
        context = browser.contexts[0]
        if not await _context_is_authenticated(context, "instagram"):
            raise RuntimeError(
                "The participant browser is not signed in to instagram"
            )
        page = await context.new_page()
        await page.goto(
            "https://www.instagram.com/accounts/edit/",
            wait_until="domcontentloaded",
            timeout=int(max(5.0, timeout_seconds) * 1000),
        )
        result = await page.evaluate(
            """
            async ({endpoints, fields, timeoutMs}) => {
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
              const attempts = [];
              const merged = {};
              const identities = new Set();

              const readEndpoint = async (endpoint) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                  const response = await fetch(endpoint, {
                    credentials: "include",
                    signal: controller.signal,
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
                        : payload && payload.data
                          && typeof payload.data.user === "object"
                            ? payload.data.user
                            : payload
                  );
                  const username = user && user.username
                    ? String(user.username).toLowerCase()
                    : "";
                  if (username) identities.add(username);
                  for (const field of fields) {
                    const value = user ? user[field] : null;
                    const populated = (
                      value !== undefined
                      && value !== null
                      && (typeof value !== "string" || value.trim() !== "")
                    );
                    if (populated && merged[field] === undefined) {
                      merged[field] = value;
                    }
                  }
                  attempts.push({
                    endpoint_path: endpoint.split("?")[0],
                    http_status: response.status
                  });
                  return response.ok;
                } catch (error) {
                  attempts.push({
                    endpoint_path: endpoint.split("?")[0],
                    http_status: 0
                  });
                  return false;
                } finally {
                  clearTimeout(timer);
                }
              };

              for (const endpoint of endpoints) {
                await readEndpoint(endpoint);
              }
              const username = typeof merged.username === "string"
                ? merged.username
                : "";
              if (username) {
                await readEndpoint(
                  "/api/v1/users/web_profile_info/?username="
                  + encodeURIComponent(username)
                );
              }
              return {
                profile: merged,
                sources: attempts,
                identity_count: identities.size
              };
            }
            """,
            {
                "endpoints": list(INSTAGRAM_OWNER_ENDPOINTS),
                "fields": list(OWNER_FIELDS),
                "timeoutMs": int(max(5.0, timeout_seconds) * 1000),
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("Instagram returned an invalid owner profile result")
        if int(result.get("identity_count") or 0) > 1:
            raise RuntimeError(
                "Instagram returned multiple account identities; no export was written"
            )
        profile = result.get("profile")
        sources = result.get("sources")
        selected = select_owner_fields(profile if isinstance(profile, dict) else {})
        if not selected.get("username"):
            raise RuntimeError(
                "No authenticated Instagram owner profile was available"
            )
        clean_sources = [
            {
                "endpoint_path": str(source.get("endpoint_path") or ""),
                "http_status": int(source.get("http_status") or 0),
            }
            for source in (sources if isinstance(sources, list) else [])
            if isinstance(source, dict)
        ]
        return selected, clean_sources
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        await playwright.stop()


async def fetch_owner_profile(
    *,
    platform: str,
    cdp_url: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized_platform = platform.strip().casefold()
    if normalized_platform not in SUPPORTED_OWNER_PLATFORMS:
        raise ValueError(f"Unsupported owner profile platform: {platform}")
    if normalized_platform != "instagram":
        return await fetch_surface_owner_profile(
            platform=normalized_platform,
            cdp_url=cdp_url,
            timeout_seconds=timeout_seconds,
        )

    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        direct_profile, direct_sources = await fetch_instagram_owner_profile(
            cdp_url=cdp_url,
            timeout_seconds=timeout_seconds,
        )
        records.append(direct_profile)
        sources.extend(direct_sources)
    except Exception as exc:
        errors.append(str(exc))
    try:
        surface_profile, surface_sources = await fetch_surface_owner_profile(
            platform=normalized_platform,
            cdp_url=cdp_url,
            timeout_seconds=timeout_seconds,
        )
        records.append(surface_profile)
        sources.extend(surface_sources)
    except Exception as exc:
        errors.append(str(exc))

    merged = merge_owner_field_records(records)
    if not merged:
        detail = "; ".join(errors) or "no owner fields were exposed"
        raise RuntimeError(f"Instagram owner profile export failed: {detail}")
    return merged, sources
