import sqlite3
import json
from pathlib import Path
from incremental_project import ingest_videos, db_path, init_db, load_session_videos

def main():
    project = "jumbo_movie"
    db_file = db_path(project)
    print(f"Connecting to database: {db_file}")

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    runs_dir = Path("comments_data") / f"project_{project}" / "raw_runs"
    for run_folder in sorted(runs_dir.iterdir()):
        if not run_folder.is_dir():
            continue


        # We don't have run_meta inside the directory natively unless we query runs table
        # We can just get run_id from the folder name timestamp or from runs table
        run_row = conn.execute("SELECT * FROM runs WHERE session_path = ?", (str(run_folder),)).fetchone()
        if not run_row:
            print(f"Could not find run for {run_folder} in db. Skipping.")
            continue

        platform = run_row["platform"]
        source_value = run_row["keyword"]
        run_id = run_row["run_id"]
        finished_at = run_row["finished_at"]

        candidates = load_session_videos(run_folder, platform)

        print(f"Ingesting {len(candidates)} videos for {platform}...")
        try:
            stats = ingest_videos(
                conn,
                project=project,
                keyword=source_value,
                platform=platform,
                videos=candidates,
                run_id=run_id,
                scraped_at=finished_at
            )
            print(f"Ingest stats: {stats}")

            conn.execute(
                """
                UPDATE runs SET discovered_count = ?, new_content_count = ?,
                    new_comment_count = ?, total_comment_count = ?
                WHERE run_id = ?
                """,
                (
                    stats["discovered_count"],
                    stats["new_content_count"],
                    stats["new_comment_count"],
                    stats["total_comment_count"],
                    run_id,
                ),
            )
            conn.commit()
            print(f"Successfully updated runs table for {run_id}")
        except Exception as e:
            print(f"Error on {run_folder.name}: {e}")

if __name__ == "__main__":
    main()
