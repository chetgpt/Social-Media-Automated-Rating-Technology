"""Export the currently authenticated participant-owned profile locally."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from tiktok_scraper.owner_profile_export import (
    SUPPORTED_OWNER_PLATFORMS,
    build_owner_export,
    fetch_owner_profile,
    owner_session_status,
    read_encrypted_export,
    write_encrypted_export,
    write_plaintext_export,
)


def _field_summary(payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get("owner_profile")
    profile = profile if isinstance(profile, dict) else {}
    demographics = payload.get("demographics")
    demographics = demographics if isinstance(demographics, dict) else {}
    geography = payload.get("geography")
    geography = geography if isinstance(geography, dict) else {}
    return {
        "platform": payload.get("platform"),
        "available_field_names": sorted(profile),
        "demographic_field_names": sorted(demographics),
        "geographic_field_names": sorted(geography),
        "private_values_printed": False,
    }


def _export_path(
    output: Path,
    *,
    platform: str,
    multiple: bool,
    plaintext: bool,
) -> Path:
    suffix = ".owner-profile.json" if plaintext else ".owner-profile.json.dpapi"
    if multiple or output.suffix == "":
        return output / f"{platform}{suffix}"
    return output


async def _export(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise ValueError(
            "Export requires --owner-confirmed after the participant signs in"
        )
    platforms = (
        list(SUPPORTED_OWNER_PLATFORMS)
        if args.platform == "all"
        else [args.platform]
    )
    output_root = Path(args.output).resolve()
    results: list[dict[str, Any]] = []
    had_error = False
    login_status = await owner_session_status(
        cdp_url=args.cdp_url,
        platforms=platforms,
    )
    for platform in platforms:
        if not login_status.get(platform):
            had_error = True
            results.append(
                {
                    "platform": platform,
                    "status": "login_required",
                    "private_values_printed": False,
                }
            )
            continue
        try:
            profile, sources = await fetch_owner_profile(
                platform=platform,
                cdp_url=args.cdp_url,
                timeout_seconds=max(5.0, args.timeout),
            )
            payload = build_owner_export(
                platform=platform,
                profile=profile,
                sources=sources,
            )
            output = _export_path(
                output_root,
                platform=platform,
                multiple=len(platforms) > 1,
                plaintext=args.plaintext,
            )
            if args.plaintext:
                write_plaintext_export(output, payload)
            else:
                write_encrypted_export(output, payload)
            summary = _field_summary(payload)
            summary.update(
                {
                    "status": "exported",
                    "output_path": str(output),
                    "encrypted": not args.plaintext,
                    "protection": (
                        "plaintext_owner_requested"
                        if args.plaintext
                        else "windows-dpapi-current-user"
                    ),
                }
            )
            results.append(summary)
        except Exception as exc:
            had_error = True
            results.append(
                {
                    "platform": platform,
                    "status": "error",
                    "error": str(exc),
                    "private_values_printed": False,
                }
            )
            if len(platforms) == 1:
                raise
    print(
        json.dumps(
            {
                "status": "partial" if had_error else "exported",
                "results": results,
                "private_values_printed": False,
            },
            indent=2,
        )
    )
    return 1 if had_error else 0


def _inspect(args: argparse.Namespace) -> int:
    input_path = Path(args.input).resolve()
    payload = read_encrypted_export(input_path)
    summary = _field_summary(payload)
    summary.update(
        {
            "status": "available",
            "input_path": str(input_path),
            "encrypted": True,
            "protection": "windows-dpapi-current-user",
        }
    )
    print(json.dumps(summary, indent=2))
    return 0


def _decrypt(args: argparse.Namespace) -> int:
    if not args.allow_plaintext:
        raise ValueError("Decrypt requires --allow-plaintext")
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    payload = read_encrypted_export(input_path)
    write_plaintext_export(output_path, payload)
    print(
        json.dumps(
            {
                "status": "decrypted",
                "output_path": str(output_path),
                "contains_private_values": True,
                "private_values_printed": False,
            },
            indent=2,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Place the currently authenticated account owner's profile data "
            "in a locally encrypted export."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument(
        "--platform",
        choices=(*SUPPORTED_OWNER_PLATFORMS, "all"),
        default="instagram",
    )
    export_parser.add_argument("--cdp-url", default="http://127.0.0.1:9231")
    export_parser.add_argument("--timeout", type=float, default=15)
    export_parser.add_argument("--output", required=True)
    export_parser.add_argument("--owner-confirmed", action="store_true")
    export_parser.add_argument(
        "--plaintext",
        action="store_true",
        help="Write owner-readable JSON instead of a Windows-protected export.",
    )

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--input", required=True)

    decrypt_parser = subparsers.add_parser("decrypt")
    decrypt_parser.add_argument("--input", required=True)
    decrypt_parser.add_argument("--output", required=True)
    decrypt_parser.add_argument("--allow-plaintext", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "export":
            return asyncio.run(_export(args))
        if args.command == "inspect":
            return _inspect(args)
        return _decrypt(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                    "private_values_printed": False,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
