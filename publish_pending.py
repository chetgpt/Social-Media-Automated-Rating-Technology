import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from tiktok_master_database import DEFAULT_MASTER_DATABASE


DEFAULT_SOCIAL_BROWSER_STATE = (
    Path("comments_data") / "social_browser" / "state.json"
)
ADAPTER_PATH = Path(__file__).resolve().with_name("tiktok_publication_adapter.py")
DEFAULT_DAILY_LIMIT = 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish pending approved TikTok drafts.")
    parser.add_argument(
        "--database",
        required=True,
        help="Exact ENGAGE SQLite database returned by the handoff command.",
    )
    parser.add_argument(
        "--social-browser-state",
        default=str(DEFAULT_SOCIAL_BROWSER_STATE),
    )
    parser.add_argument(
        "--master-database",
        default=str(DEFAULT_MASTER_DATABASE),
        help=(
            "Workspace-wide TikTok comment history database used to prevent "
            "repeat comments across project runs."
        ),
    )
    parser.add_argument(
        "--browser-startup-timeout",
        type=float,
        default=120.0,
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--publication-id",
        help="Process exactly this approved TikTok publication.",
    )
    selection.add_argument(
        "--all-approved",
        action="store_true",
        help=(
            "Process every approved TikTok publication in one explicitly "
            "selected ENGAGE run, sequentially."
        ),
    )
    parser.add_argument(
        "--engage-run-id",
        help="Required scope for --all-approved; ignored selections are rejected.",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=DEFAULT_DAILY_LIMIT,
        help=(
            "Optional maximum verified TikTok publications for this project "
            "per local day; 0 disables the cap (default)."
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum attempts allowed for the selected publication.",
    )
    parser.add_argument(
        "--inter-publication-delay",
        type=float,
        default=0.0,
        help=(
            "Optional delay in seconds between sequential live publications; "
            "0 publishes the next approved response immediately."
        ),
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help=(
            "Continue to later approved responses after one adapter failure. "
            "By default the batch stops safely and leaves later rows approved "
            "for a resume."
        ),
    )
    parser.add_argument(
        "--showcase-auto-prepare-config",
        default="",
        help=(
            "Optional path to the explicit, non-secret showcase media "
            "configuration. After a confirmed comment receipt, the adapter "
            "captures the exact comment and prepares its API-ready JPEG; it "
            "does not draft, authorize, or publish the showcase."
        ),
    )
    args = parser.parse_args(argv)
    if args.all_approved and not str(args.engage_run_id or "").strip():
        parser.error("--all-approved requires --engage-run-id")
    if not args.all_approved and args.engage_run_id:
        parser.error("--engage-run-id is only valid with --all-approved")
    if args.daily_limit < 0:
        parser.error("--daily-limit must be 0 (unlimited) or a positive integer")
    if args.max_attempts <= 0:
        parser.error("--max-attempts must be positive")
    if args.inter_publication_delay < 0:
        parser.error("--inter-publication-delay cannot be negative")
    return args


def select_approved_publications(
    database: Path,
    *,
    publication_id: str = "",
    all_approved: bool = False,
    engage_run_id: str = "",
) -> list[sqlite3.Row]:
    publication_id = str(publication_id or "").strip()
    engage_run_id = str(engage_run_id or "").strip()
    if bool(publication_id) == bool(all_approved):
        raise RuntimeError(
            "Select exactly one publication with --publication-id, or select "
            "an explicit run with --all-approved --engage-run-id."
        )
    if all_approved and not engage_run_id:
        raise RuntimeError("--all-approved requires an explicit ENGAGE run ID.")
    if not all_approved and engage_run_id:
        raise RuntimeError("An ENGAGE run ID is only valid with --all-approved.")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT publication_id, target_url, mode
            FROM publication_queue
            WHERE status = 'approved' AND platform = 'tiktok'
              AND COALESCE(engage_run_id, '') <> ''
              AND COALESCE(engage_post_id, '') <> ''
        """
        if publication_id:
            query += " AND publication_id = ?"
            parameters = (publication_id,)
        else:
            query += " AND engage_run_id = ?"
            parameters = (engage_run_id,)
        query += " ORDER BY created_at, publication_id"
        rows = conn.execute(query, parameters).fetchall()
    finally:
        conn.close()

    if not rows:
        selection = (
            f"publication {publication_id}"
            if publication_id
            else f"ENGAGE run {engage_run_id}"
        )
        raise RuntimeError(f"No approved TikTok publications found for {selection}")
    return rows


def build_adapter_command(
    *,
    database: Path,
    publication_id: str,
    social_browser_state: str | Path,
    execute: bool,
    master_database: str | Path = DEFAULT_MASTER_DATABASE,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
    max_attempts: int = 3,
    browser_startup_timeout: float = 120.0,
    showcase_auto_prepare_config: str | Path = "",
) -> list[str]:
    command = [
        sys.executable,
        str(ADAPTER_PATH),
        "--database",
        str(database),
        "--publication-id",
        publication_id,
        "--master-database",
        str(Path(master_database)),
        "--social-browser-state",
        str(Path(social_browser_state)),
        "--browser-startup-timeout",
        str(float(browser_startup_timeout)),
        "--daily-limit",
        str(daily_limit),
        "--max-attempts",
        str(max_attempts),
    ]
    if str(showcase_auto_prepare_config or "").strip():
        command.extend(
            [
                "--showcase-auto-prepare-config",
                str(Path(showcase_auto_prepare_config)),
            ]
        )
    if execute:
        command.append("--execute")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.database)
    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        return 1

    try:
        rows = select_approved_publications(
            db_path,
            publication_id=args.publication_id or "",
            all_approved=args.all_approved,
            engage_run_id=args.engage_run_id or "",
        )
    except (RuntimeError, sqlite3.Error) as exc:
        print(f"Error: {exc}")
        return 1

    if not rows:
        print("No approved TikTok drafts found in the publication queue.")
        return 0

    print("The adapter will start/reuse and verify Edge Profile 7.")
    action = "publish" if args.execute else "dry-run"
    print(f"Selected {len(rows)} approved TikTok draft(s) for {action}.")
    if args.daily_limit:
        print(f"Configured project daily limit: {args.daily_limit}.")
    else:
        print(
            "Project daily limit disabled; every selected response will still "
            "pass through its per-publication gates."
        )

    success_count = 0
    showcase_captured_count = 0
    showcase_enqueued_count = 0
    showcase_prepared_count = 0
    showcase_attention_count = 0
    for index, row in enumerate(rows):
        pub_id = row["publication_id"]
        target_url = row["target_url"]
        mode = row["mode"]

        print(f"\nProcessing publication {pub_id} for {target_url} (mode: {mode})...")

        cmd = build_adapter_command(
            database=db_path,
            publication_id=str(pub_id),
            social_browser_state=args.social_browser_state,
            execute=args.execute,
            master_database=args.master_database,
            daily_limit=args.daily_limit,
            max_attempts=args.max_attempts,
            browser_startup_timeout=args.browser_startup_timeout,
            showcase_auto_prepare_config=args.showcase_auto_prepare_config,
        )

        adapter_succeeded = False
        try:
            result = subprocess.run(cmd, check=True, text=True, capture_output=True)
            print("Adapter completed successfully.")
            output = None
            try:
                output = json.loads(result.stdout)
                print(f"Receipt ID: {output.get('receipt_id')}")
                print(f"Result Status: {output.get('status')}")
            except json.JSONDecodeError:
                print("Adapter output:")
                print(result.stdout)
            if args.execute:
                adapter_succeeded = (
                    isinstance(output, dict)
                    and output.get("status") == "published"
                )
                if not adapter_succeeded:
                    print(
                        "Live adapter did not return a verified published "
                        "outcome; treating this item as failed."
                    )
                elif isinstance(output, dict):
                    screenshot = output.get("comment_screenshot")
                    showcase = output.get("comment_showcase")
                    preparation = output.get("showcase_preparation")
                    if isinstance(screenshot, dict) and screenshot:
                        showcase_captured_count += 1
                    if (
                        isinstance(showcase, dict)
                        and str(showcase.get("showcase_id") or "")
                    ):
                        showcase_enqueued_count += 1
                    preparation_status = (
                        str(preparation.get("status") or "")
                        if isinstance(preparation, dict)
                        else ""
                    )
                    if preparation_status == "publish_media_ready":
                        showcase_prepared_count += 1
                    auxiliary_errors = {
                        "capture": str(
                            output.get("showcase_capture_error") or ""
                        ).strip(),
                        "enqueue": str(
                            output.get("showcase_enqueue_error") or ""
                        ).strip(),
                        "preparation": str(
                            output.get("showcase_preparation_error") or ""
                        ).strip(),
                    }
                    auxiliary_errors = {
                        key: value
                        for key, value in auxiliary_errors.items()
                        if value
                    }
                    if auxiliary_errors:
                        showcase_attention_count += 1
                        print(
                            "Comment publication is confirmed, but its "
                            "showcase needs auxiliary recovery: "
                            + "; ".join(
                                f"{key}={value}"
                                for key, value in auxiliary_errors.items()
                            )
                        )
                        if auxiliary_errors.get("capture"):
                            print(
                                "Recovery target: "
                                f"publication_id={pub_id}, "
                                f"receipt_id={output.get('receipt_id')}"
                            )
                    elif preparation_status in {
                        "not_configured",
                        "disabled",
                    }:
                        showcase_attention_count += 1
                        print(
                            "Showcase screenshot is queued but not media-ready; "
                            "automatic JPEG preparation is "
                            f"{preparation_status} and needs attention."
                        )
            else:
                adapter_succeeded = True
        except subprocess.CalledProcessError as e:
            print(f"Failed to execute adapter for {pub_id}.")
            print(f"Exit code: {e.returncode}")
            print(f"Stdout:\n{e.stdout}")
            print(f"Stderr:\n{e.stderr}")
        if adapter_succeeded:
            success_count += 1
        elif not args.continue_on_error:
            remaining = len(rows) - index - 1
            print(
                "Stopping the batch after the first unverified outcome; "
                f"{remaining} later approved response(s) remain available "
                "for resume."
            )
            break
        if (
            args.execute
            and args.inter_publication_delay > 0
            and index < len(rows) - 1
        ):
            print(
                "Waiting "
                f"{args.inter_publication_delay:g}s before the next publication."
            )
            time.sleep(args.inter_publication_delay)

    print(f"\nFinished processing. {success_count}/{len(rows)} successful.")
    if args.execute:
        print(
            "Showcase auxiliary results: "
            f"captured={showcase_captured_count}, "
            f"enqueued={showcase_enqueued_count}, "
            f"publish_media_ready={showcase_prepared_count}, "
            f"needs_attention={showcase_attention_count}."
        )
    return 0 if success_count == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
